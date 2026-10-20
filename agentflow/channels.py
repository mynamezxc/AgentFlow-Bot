import base64
import hashlib
import hmac
import json
import mimetypes
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from .providers import http_json
from .store import DomainError, now, uid


class Channels:
    def publish(self, post_id):
        with self.guard:
            post = self.store.get("posts", post_id)
            if post["status"] != "approved" or post["channel"] != "facebook":
                raise DomainError("Only approved Facebook content can be published.", 409)
            settings = self.store.settings()
            token = self.vault.get("FACEBOOK_PAGE_TOKEN")
            if not token or not settings.get("page_id"):
                self.store.patch("posts", post_id, {"error": 'Facebook Page and token are not configured.'})
                return self.store.get("posts", post_id)
            version = settings.get("graph_version", "")
            if not re.fullmatch(r"v\d+\.\d+", version):
                raise DomainError("Invalid Graph API version.")
            if post.get("media_type") in ("image", "video") and not post.get("media_url"):
                raise DomainError("Image and video posts require an HTTPS media URL.")
            if not self.store.reserve_delivery("publish:" + post_id):
                raise DomainError("This post has already been submitted. Reconcile the result before publishing again.", 409)
            self.store.patch("posts", post_id, {"status": "publishing"})
        try:
            route, payload = "feed", {"message": post["body"]}
            if post.get("media_type") == "image":
                route, payload = "photos", {"url": post["media_url"], "caption": post["body"]}
            if post.get("media_type") == "video":
                route, payload = "videos", {"file_url": post["media_url"], "description": post["body"]}
            result = http_json(f"https://graph.facebook.com/{version}/{settings['page_id']}/{route}", payload,
                               {"Authorization": "Bearer " + token})
            if not result.get("id") and not result.get("post_id"):
                raise DomainError("Facebook did not return a post ID.", 502)
            return self.store.patch("posts", post_id, {"status": "published", "external_id": result.get("post_id") or result["id"],
                                                      "published_at": now(), "error": ""})
        except DomainError as exc:
            return self.store.patch("posts", post_id, {"status": "unknown", "error": str(exc) + ' Reconcile on Facebook before retrying.'})

    TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024

    def telegram_document(self, token, chat_id, path, caption=""):
        """sendDocument as multipart/form-data (stdlib only)."""
        boundary = "----agentflow" + uid("b")
        name = re.sub(r'[\r\n"\\]', "_", path.name)
        fields = [("chat_id", str(chat_id))] + ([("caption", caption[:1000])] if caption else [])
        body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\n'
                 f'Content-Type: {mimetypes.guess_type(name)[0] or "application/octet-stream"}\r\n\r\n').encode()
        body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", data=body,
                                         headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise DomainError(f"Telegram returned HTTP {exc.code} for the attachment.", 502) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise DomainError("Cannot connect to Telegram to send the attachment.", 502) from None

    def reply_attachments(self, artifact_ids):
        """([(title, path)] small enough to send, [text lines] for files that can only be mentioned)."""
        documents, mentions = [], []
        for artifact_id in artifact_ids or []:
            try:
                item, path = self.outputs.resolve("artifact", artifact_id)
                if path.stat().st_size > self.TELEGRAM_FILE_LIMIT:
                    raise DomainError("too large")
                documents.append((item.get("title") or path.name, path))
            except (DomainError, OSError):
                try:
                    item = self.store.get("artifacts", artifact_id)
                    mentions.append((item.get("title") or item.get("path") or artifact_id) + " (" + str(item.get("path", "")) + ") - open it in AgentFlow")
                except DomainError:
                    continue
        return documents, mentions

    def send_reply(self, outbound, content, artifact_ids=None):
        key = outbound["event_key"] + ":reply"
        if not self.store.reserve_delivery(key):
            return "duplicate"
        try:
            documents, mentions = self.reply_attachments(artifact_ids)
            if outbound["channel"] == "telegram":
                token = self.vault.get("TELEGRAM_BOT_TOKEN")
                text = content[:4000]
                if mentions:
                    text = (content[:3800] + "\n\nFiles:\n" + "\n".join(mentions))[:4000]
                if text.strip() or not documents:
                    http_json(f"https://api.telegram.org/bot{token}/sendMessage",
                              {"chat_id": outbound["recipient"], "text": text})
                for title, path in documents:
                    try:
                        self.telegram_document(token, outbound["recipient"], path, title)
                    except DomainError:
                        self.store.event("Attachment not delivered", title + " could not be sent to Telegram.", "error")
            else:
                settings = self.store.settings()
                listed = [t for t, _ in documents] + mentions
                text = content[:1900] if not listed else (content[:1600] + "\n\nFiles: " + ", ".join(listed))[:1900]
                http_json(f"https://graph.facebook.com/{settings['graph_version']}/{settings['page_id']}/messages",
                          {"recipient": {"id": outbound["recipient"]}, "messaging_type": "RESPONSE",
                           "message": {"text": text}},
                          {"Authorization": "Bearer " + self.vault.get("FACEBOOK_PAGE_TOKEN")})
            self.store.delivery_state(key, "sent")
            return "sent"
        except DomainError:
            self.store.delivery_state(key, "unknown")
            self.store.event('Reply delivery unconfirmed', 'Check channel messages before retrying.', "error")
            return "unknown"

    def queue_reply(self, outbound, content, event_key, artifact_ids=None):
        return self.store.create("channel_outbox", {"outbound": {**outbound, "event_key": event_key},
            "content": str(content)[:4000], "status": "queued", **({"artifact_ids": list(artifact_ids)[:10]} if artifact_ids else {})})

    def deliver_outbox(self, item):
        try:
            outcome = self.send_reply(item["outbound"], item["content"], item.get("artifact_ids"))
            self.store.patch("channel_outbox", item["id"], {"status": outcome or "unknown", "completed_at": now()})
        except Exception:
            self.store.patch("channel_outbox", item["id"], {"status": "unknown"})

    def dispatch_outbox(self):
        busy = {(m["outbound"]["channel"], m["outbound"]["recipient"]) for m in self.store.all("channel_outbox") if m["status"] == "sending"}
        for item in self.store.all("channel_outbox"):
            if item["outbound"]["channel"] == "telegram_group":
                continue  # delivered by the Telegram bridge's own sender (ordering, 429 retries, edits)
            target = (item["outbound"]["channel"], item["outbound"]["recipient"])
            if item["status"] == "queued" and target not in busy:
                busy.add(target)
                self.store.patch("channel_outbox", item["id"], {"status": "sending"})
                self.pool.submit(self.deliver_outbox, item)

    def webhook(self, channel, raw, headers):
        settings = self.store.settings()
        if channel == "telegram":
            secret = self.vault.get("TELEGRAM_WEBHOOK_SECRET")
            supplied = headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not secret or not hmac.compare_digest(secret, supplied):
                raise DomainError("Webhook authentication failed.", 401)
            data = json.loads(raw)
            message = data.get("message", {})
            recipient = str(message.get("chat", {}).get("id", ""))
            allowed = [x.strip() for x in settings.get("telegram_chats", "").split(",") if x.strip()]
            if not allowed or recipient not in allowed:
                raise DomainError("This Telegram chat is not authorized.", 403)
            text = message.get("text")
            if not text:
                return {"ignored": True}
            key = "telegram:" + str(data["update_id"])
            agent_id, kind = settings.get("telegram_agent_id", "a-lead"), "chat"
            events = [(key, recipient, text)]
        elif channel == "messenger":
            secret = self.vault.get("FACEBOOK_APP_SECRET")
            signature = headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            if not secret or not hmac.compare_digest(expected, signature):
                raise DomainError("Invalid Meta signature.", 401)
            if not settings.get("messenger_enabled"):
                return {"ignored": True, "reason": 'Automatic replies are disabled'}
            data = json.loads(raw)
            agent_id, kind = settings.get("support_agent_id", "a-support"), "support"
            events = []
            for entry in data.get("entry", []):
                if str(entry.get("id")) != settings.get("page_id"):
                    continue
                for event in entry.get("messaging", []):
                    message = event.get("message", {})
                    if message.get("text") and message.get("mid") and not message.get("is_echo"):
                        age = time.time() - event.get("timestamp", 0) / 1000
                        if 0 <= age < 23 * 3600:
                            events.append(("messenger:" + message["mid"], str(event["sender"]["id"]), message["text"]))
        else:
            secret = self.vault.get("SEO_WEBHOOK_SECRET")
            if not secret or not hmac.compare_digest("Bearer " + secret, headers.get("Authorization", "")):
                raise DomainError("SEO webhook authentication failed.", 401)
            data = json.loads(raw)
            key = "seo:" + str(data.get("event_id", ""))
            if not data.get("event_id") or not data.get("prompt"):
                raise DomainError("A unique event_id and prompt are required.")
            events = [(key, "", data["prompt"])]
            agent_id, kind = "a-seo", "schedule"
        queued = []
        for key, recipient, text in events:
            with self.guard:
                agent = self.store.get("agents", agent_id)
                if not settings.get("execution_enabled") or not agent.get("model") or agent["status"] == "paused":
                    raise DomainError("The agent is not ready to receive webhooks.", 409)
                if not self.store.reserve_delivery(key):
                    continue
                conversation = channel + (":" + recipient if recipient else "")
                if channel == "telegram" and str(text).startswith("/answer "):
                    parts = str(text).split(" ", 2)
                    if len(parts) < 3:
                        raise DomainError("Use /answer <question ID> <answer>.")
                    decision = self.store.get("decisions", parts[1])
                    if decision.get("channel") != conversation:
                        raise DomainError("This question does not belong to this conversation.", 403)
                    self.team.answer(parts[1], {"answer": parts[2]})
                    continue
                outbound = {"channel": channel, "recipient": recipient, "event_key": key} if recipient else None
                if kind != "support":
                    run = self.chat({"agent_id": agent_id, "channel": conversation, "message": str(text)[:20000]})
                    self.store.patch("runs", run["id"], {"kind": kind, "outbound": outbound, "owner_outbound": outbound})
                    queued.append(run["id"])
                    continue
                run = self.enqueue(agent_id, str(text)[:20000], kind, channel=conversation,
                                   **({"outbound": {"channel": channel, "recipient": recipient, "event_key": key}} if recipient else {}))
                self.store.create("messages", {"agent_id": agent_id, "role": "user", "content": str(text)[:20000],
                                              "channel": conversation, "run_id": run["id"]})
                queued.append(run["id"])
        return {"queued": queued}

    def generate_image(self, data):
        """Owner-initiated image generation (see images.ImageEngines); {prompt} alone stays valid."""
        return self.images.generate(data, source="owner")
