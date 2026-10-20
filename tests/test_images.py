import base64
import http.client
import io
import json
import os
import stat
import struct
import sys
import threading
import unittest
import urllib.error
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import test_runtime as runtime
from agentflow.accounts import ProviderLimit
from agentflow.images import (CODEX_NOTICE, DEFAULTS, STYLES, ImageEngines, Ref, dimensions, sniff)
from agentflow.store import DomainError
from server import make_handler


def png(width=2, height=2, extra=b""):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    row = b"\x00" + b"\x00\xff\x00" * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"") + extra)


FAKE_CODEX = '''#!{python}
import base64, json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
if os.environ.get("FAKE_LOG"):
    open(os.environ["FAKE_LOG"], "w").write(json.dumps({{"args": args, "prompt": prompt, "cwd": os.getcwd()}}))
mode = os.environ.get("FAKE_MODE", "file")
png = base64.b64decode(os.environ["FAKE_PNG"])
home = os.environ["CODEX_HOME"]
print(json.dumps({{"type": "thread.started", "thread_id": "t1"}}))
if mode == "file":
    folder = os.path.join(home, "generated_images", "t1")
    os.makedirs(folder, exist_ok=True)
    for i in range(int(os.environ.get("FAKE_COUNT", "1"))):
        open(os.path.join(folder, "ig_%d.png" % i), "wb").write(png + b"\\0" * i)
elif mode == "cwd":
    open("out.png", "wb").write(png)
elif mode == "b64":
    print(json.dumps({{"type": "item.completed", "item": {{"type": "image_generation_call", "result": base64.b64encode(png).decode()}}}}))
elif mode == "limit":
    print(json.dumps({{"type": "turn.failed", "error": {{"message": "You have hit your usage limit. Try again later."}}}}))
    sys.exit(1)
elif mode == "none":
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "I cannot create images."}}}}))
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "done"}}}}))
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 2}}}}))
'''


class Resp:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.raw


def http_error(code, message="bad"):
    return urllib.error.HTTPError("https://x", code, message, {}, io.BytesIO(json.dumps({"error": {"message": message}}).encode()))


class ImageTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown

    def agent(self, provider, model):
        self.store.patch("agents", "a-lead", {"provider": provider, "model": model, "binding_origin": "owner"})
        return self.store.get("agents", "a-lead")

    def image_setting(self, **value):
        return self.service.settings({"image": value})["image"]

    def fake_codex(self, **env):
        path = self.root / "fake-codex"
        path.write_text(FAKE_CODEX.format(python=sys.executable))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir(exist_ok=True)
        self.log = self.root / "codex-call.json"
        patcher = patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home), "FAKE_PNG": base64.b64encode(png()).decode(),
                                          "FAKE_LOG": str(self.log), **env})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.providers.cli["codex"] = str(path)
        self.providers.accounts.check("codex", force=True)
        self.providers.models += [{"provider": "codex", "id": "gpt-5.5", "name": "GPT-5.5"}]

    def gemini_key(self, models=True):
        self.vault.set({"GEMINI_API_KEY": "gem-key-123"})
        if models:
            self.providers.models += [
                {"provider": "gemini", "id": "gemini-nano-banana-2.1", "capabilities": ["image"]},
                {"provider": "gemini", "id": "gemini-3.1-flash-image", "capabilities": ["image"]},
                {"provider": "gemini", "id": "gemini-2.5-pro", "capabilities": ["chat"]}]

    def gateway(self):
        self.store.settings({"gateway_url": "https://gateway.example/v1"})
        self.providers.models += [{"provider": "gateway", "id": "gpt-image-1"}, {"provider": "gateway", "id": "chat-model"}]

    def project(self):
        return self.store.create("projects", {"title": "P", "status": "active"})

    # ---- resolution -------------------------------------------------------------------------------
    @unittest.skipIf(os.name == "nt", "fake codex is a POSIX script")
    def test_resolution_matrix(self):
        images = self.engine.images
        self.fake_codex()
        self.gemini_key()
        self.gateway()
        codex_agent = self.agent("codex", "gpt-5.5")
        choice = images.resolve(codex_agent)
        self.assertEqual((choice.engine, choice.model, choice.via), ("codex", "gpt-5.5", "agent"))
        self.assertIn("Codex plan", choice.notice)
        gemini_agent = self.agent("gemini", "gemini-2.5-pro")
        choice = images.resolve(gemini_agent)
        self.assertEqual((choice.engine, choice.model), ("gemini", "gemini-3.1-flash-image"))  # preferred over nano-banana
        claude_agent = self.agent("claude", "sonnet")
        self.assertEqual(images.resolve(claude_agent).engine, "codex")                           # codex signed in first
        self.image_setting(fallback=[{"engine": "gateway", "model": "gpt-image-1"}, {"engine": "gemini"}])
        choice = images.resolve(claude_agent)
        self.assertEqual((choice.engine, choice.model, choice.via), ("gateway", "gpt-image-1", "fallback"))
        self.image_setting(fallback=[])
        # Manual calls without an agent follow Atlas.
        self.agent("codex", "gpt-5.5")
        self.assertEqual(images.resolve(None).engine, "codex")
        # A Gemini agent without a key falls through and says why.
        self.vault.set({}, replace_prefix="GEMINI_API_KEY")
        self.vault.values.pop("GEMINI_API_KEY", None)
        choice = images.resolve(gemini_agent)
        self.assertEqual(choice.engine, "codex")
        self.assertIn("used Codex", choice.notice)
        # The owner's fixed engine wins over the agent, and never falls back.
        self.image_setting(mode="fixed", engine="gateway", model="gpt-image-1")
        self.assertEqual(images.resolve(codex_agent).engine, "gateway")
        self.assertEqual(images.resolve(codex_agent, request={"engine": "codex"}, source="agent").engine, "gateway")
        self.assertEqual(images.resolve(codex_agent, request={"engine": "codex"}, source="owner").engine, "codex")
        self.image_setting(mode="fixed", engine="gemini", model="gemini-3.1-flash-image")
        with self.assertRaises(DomainError) as ctx:
            images.resolve(codex_agent)
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("Gemini API key", str(ctx.exception))
        # An agent may choose the engine only in agent mode.
        self.image_setting(mode="agent")
        self.gemini_key(models=False)
        self.assertEqual(images.resolve(codex_agent, request={"engine": "gemini", "model": "gemini-nano-banana-2.1"}, source="agent").model,
                         "gemini-nano-banana-2.1")

    def test_nothing_available_names_what_to_configure(self):
        self.agent("claude", "sonnet")
        with self.assertRaises(DomainError) as ctx:
            self.engine.images.resolve(self.store.get("agents", "a-lead"))
        message = str(ctx.exception)
        for needle in ("Codex", "Gemini API key", "gateway", "Settings > Image generation"):
            self.assertIn(needle, message)

    @unittest.skipIf(os.name == "nt", "fake codex is a POSIX script")
    def test_reference_images_skip_engines_without_support(self):
        self.fake_codex()
        self.gateway()
        self.image_setting(fallback=[{"engine": "gateway", "model": "gpt-image-1"}])
        agent = self.agent("claude", "sonnet")
        self.assertEqual(self.engine.images.resolve(agent, refs=0).engine, "gateway")
        self.assertEqual(self.engine.images.resolve(agent, refs=2).engine, "codex")

    @unittest.skipIf(os.name == "nt", "fake codex is a POSIX script")
    def test_engine_listing_shape(self):
        self.fake_codex()
        self.gateway()
        self.agent("codex", "gpt-5.5")
        listing = self.engine.images.listing()
        engines = {e["id"]: e for e in listing["engines"]}
        self.assertEqual(set(engines), {"codex", "gemini", "gateway"})
        self.assertTrue(engines["codex"]["available"])
        self.assertIn("Codex plan", engines["codex"]["notice"])
        self.assertTrue(any(m.get("default") and m["id"] == "gpt-5.5" for m in engines["codex"]["models"]))
        self.assertFalse(engines["gemini"]["available"])
        self.assertIn("API key", engines["gemini"]["reason"])
        self.assertTrue(engines["gateway"]["available"])
        for engine in engines.values():
            for key in ("aspect_ratios", "sizes", "formats", "max_count", "max_references"):
                self.assertIn(key, engine["params"])
        self.assertEqual(listing["settings"]["mode"], "agent")
        self.assertEqual(listing["settings"]["defaults"], DEFAULTS)
        self.assertIn("watercolor", [s["id"] for s in listing["styles"]])

    # ---- Codex ------------------------------------------------------------------------------------
    @unittest.skipIf(os.name == "nt", "fake codex is a POSIX script")
    def test_codex_engine_registers_artifacts_and_uses_imagegen_flags(self):
        self.fake_codex(FAKE_COUNT="2")
        old = self.codex_home / "generated_images" / "old"
        old.mkdir(parents=True)
        (old / "stale.png").write_bytes(png(9, 9))
        os.utime(old / "stale.png", (1000, 1000))
        project = self.project()
        agent = self.agent("codex", "gpt-5.5")
        run = {"id": "run-img", "project_id": project["id"]}
        result = self.engine.tool(agent, {"name": "image.generate", "arguments": {
            "prompt": "a red apple", "aspect_ratio": "3:2", "size": "2K", "quality": "high", "count": 2, "style": "photo",
            "negative": "text, watermark"}}, run)
        call = json.loads(self.log.read_text())
        args = call["args"]
        self.assertEqual(args[0], "exec")
        self.assertIn("--enable", args)
        self.assertEqual(args[args.index("--enable") + 1], "image_generation")
        self.assertNotIn("image_generation", [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--disable"])
        self.assertEqual(args[args.index("--model") + 1], "gpt-5.5")
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        for flag in ("--skip-git-repo-check", "--ephemeral", "--json"):
            self.assertIn(flag, args)
        self.assertTrue(call["prompt"].startswith("$imagegen"))
        for hint in ("a red apple", "Aspect ratio: 3:2", "2048 px", "Quality: high", "exactly 2", "Photorealistic", "Avoid: text, watermark"):
            self.assertIn(hint, call["prompt"])
        self.assertEqual((result["engine"], result["model"]), ("codex", "gpt-5.5"))
        self.assertEqual(len(result["images"]), 2)            # the stale pre-existing file is not collected
        self.assertIn(CODEX_NOTICE, result["notice"])
        for image in result["images"]:
            artifact = self.store.get("artifacts", image["artifact_id"])
            self.assertTrue(artifact["is_image"])
            self.assertEqual((artifact["project_id"], artifact["agent_id"], artifact["run_id"]), (project["id"], "a-lead", "run-img"))
            self.assertTrue(Path(artifact["absolute_path"]).is_file())
            self.assertTrue(image["url"].startswith("/media/image-"))
            self.assertEqual((image["width"], image["height"]), (2, 2))
        self.assertEqual(result["artifact_id"], result["images"][0]["artifact_id"])
        self.assertEqual(result["path"], result["images"][0]["path"])

    @unittest.skipIf(os.name == "nt", "fake codex is a POSIX script")
    def test_codex_references_are_passed_as_image_flags(self):
        self.fake_codex()
        project = self.project()
        agent = self.agent("codex", "gpt-5.5")
        run = {"id": "run-ref", "project_id": project["id"]}
        first = self.engine.images.generate({"prompt": "base"}, actor=agent, run=run, source="agent")
        second = self.engine.images.generate({"prompt": "variation", "reference_artifact_ids": [first["artifact_id"]]},
                                             actor=agent, run=run, source="agent")
        args = json.loads(self.log.read_text())["args"]
        self.assertEqual(args[args.index("--image") + 1], str(self.engine.workspace / first["path"]))
        self.assertIn("reference image", json.loads(self.log.read_text())["prompt"])
        self.assertEqual(len(second["images"]), 1)
        other = self.store.create("projects", {"title": "Other", "status": "active"})
        with self.assertRaises(DomainError) as ctx:
            self.engine.images.generate({"prompt": "x", "reference_artifact_ids": [first["artifact_id"]]}, actor=agent,
                                        run={"id": "other", "project_id": other["id"]}, source="agent")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(DomainError):
            self.engine.images.generate({"prompt": "x", "reference_artifact_ids": ["missing"]}, actor=agent, run=run, source="agent")

    @unittest.skipIf(os.name == "nt", "fake codex is a POSIX script")
    def test_codex_base64_event_and_working_directory_images(self):
        for mode in ("b64", "cwd"):
            with self.subTest(mode=mode):
                self.fake_codex(FAKE_MODE=mode)
                agent = self.agent("codex", "gpt-5.5")
                result = self.engine.images.generate({"prompt": "icon"}, actor=agent, source="agent")
                self.assertEqual(len(result["images"]), 1)
                self.assertTrue(sniff((self.engine.workspace / result["path"]).read_bytes()))

    @unittest.skipIf(os.name == "nt", "fake codex is a POSIX script")
    def test_codex_limit_and_empty_results_are_clear_errors(self):
        agent = self.agent("codex", "gpt-5.5")
        self.fake_codex(FAKE_MODE="limit")
        with self.assertRaises(ProviderLimit) as ctx:
            self.engine.images.generate({"prompt": "x"}, actor=agent, source="agent")
        self.assertEqual(ctx.exception.status, 429)
        self.fake_codex(FAKE_MODE="none")
        with self.assertRaises(DomainError) as ctx:
            self.engine.images.generate({"prompt": "x"}, actor=agent, source="agent")
        self.assertIn("produced no image", str(ctx.exception))
        self.assertIn("I cannot create images", str(ctx.exception))

    # ---- Gemini -----------------------------------------------------------------------------------
    def gemini_router(self, calls, interactions=None, generate=None, models=None):
        raw = base64.b64encode(png(3, 2)).decode()

        def route(req, timeout=None):
            body = json.loads(req.data) if req.data else None
            calls.append({"url": req.full_url, "body": body, "headers": {k.lower(): v for k, v in req.header_items()}})
            if req.full_url.startswith("https://generativelanguage.googleapis.com/v1beta/models?"):
                return Resp({"models": models if models is not None else [
                    {"name": "models/gemini-3.1-flash-image"}, {"name": "models/gemini-3.1-flash-lite-image"},
                    {"name": "models/gemini-2.5-pro"}, {"name": "models/imagen-4.0-generate-001"}]})
            if req.full_url.endswith("/v1beta/interactions"):
                if interactions:
                    return interactions(req)
                return Resp({"output_image": {"data": raw, "mime_type": "image/png"}})
            if req.full_url.endswith(":generateContent"):
                if generate:
                    return generate(req)
                return Resp({"candidates": [{"content": {"parts": [{"text": "ok"}, {"inlineData": {"mimeType": "image/png", "data": raw}}]}}]})
            raise AssertionError(req.full_url)
        return patch("agentflow.images.urllib.request.urlopen", side_effect=route)

    def test_gemini_catalog_comes_from_the_api_and_is_cached(self):
        self.gemini_key(models=False)
        calls = []
        with self.gemini_router(calls):
            self.assertEqual(self.engine.images.gemini_models(), ["gemini-3.1-flash-image", "gemini-3.1-flash-lite-image"])
            self.engine.images.gemini_models()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["headers"]["x-goog-api-key"], "gem-key-123")
        gemini = next(e for e in self.engine.images.listing()["engines"] if e["id"] == "gemini")
        self.assertTrue(gemini["available"])
        lite = next(m for m in gemini["models"] if m["id"] == "gemini-3.1-flash-lite-image")
        self.assertEqual(lite["sizes"], ["1K"])

    def test_gemini_interactions_request_references_and_count(self):
        self.gemini_key()
        calls = []
        agent = self.agent("gemini", "gemini-2.5-pro")
        base = self.engine.images.save([png()], type("C", (), {"engine": "x", "model": "m"}), {"style": "none", "aspect_ratio": "1:1"},
                                       "ref", agent, {}, None, None)[0]
        with self.gemini_router(calls):
            result = self.engine.images.generate({"prompt": "a lighthouse", "aspect_ratio": "16:9", "size": "512px", "format": "jpeg",
                                                  "count": 2, "reference_artifact_ids": [base["artifact_id"]], "style": "cinematic"},
                                                 actor=agent, source="agent")
        posts = [c for c in calls if c["url"].endswith("/interactions")]
        self.assertEqual(len(posts), 2)
        body = posts[0]["body"]
        self.assertEqual(body["model"], "gemini-3.1-flash-image")
        self.assertEqual(body["response_format"], {"type": "image", "mime_type": "image/jpeg", "aspect_ratio": "16:9", "image_size": "512px"})
        self.assertEqual(body["input"][0]["type"], "text")
        self.assertIn("a lighthouse", body["input"][0]["text"])
        self.assertIn("Cinematic still", body["input"][0]["text"])
        self.assertEqual(body["input"][1]["type"], "image")
        self.assertEqual(base64.b64decode(body["input"][1]["data"]), png())
        self.assertEqual(posts[0]["headers"]["x-goog-api-key"], "gem-key-123")
        self.assertEqual((result["engine"], len(result["images"])), ("gemini", 2))
        self.assertEqual((result["images"][0]["width"], result["images"][0]["height"]), (3, 2))
        self.assertNotIn("notice", result)

    def test_gemini_falls_back_to_generate_content(self):
        self.gemini_key()
        calls = []
        agent = self.agent("gemini", "gemini-2.5-pro")

        def unknown(req):
            raise http_error(400, "Invalid JSON payload received. Unknown name \"response_format\"")
        with self.gemini_router(calls, interactions=unknown):
            result = self.engine.images.generate({"prompt": "a fox", "aspect_ratio": "4:5", "count": 2}, actor=agent, source="agent")
        generated = [c for c in calls if c["url"].endswith(":generateContent")]
        self.assertEqual(len(generated), 2)
        self.assertEqual(len([c for c in calls if c["url"].endswith("/interactions")]), 1)   # remembered after the first miss
        config = generated[0]["body"]["generationConfig"]
        self.assertEqual(config["responseModalities"], ["IMAGE"])
        self.assertEqual(config["imageConfig"], {"aspectRatio": "4:5", "imageSize": "1K"})
        self.assertIn("gemini-3.1-flash-image:generateContent", generated[0]["url"])
        self.assertEqual(len(result["images"]), 2)

    def test_gemini_validation_and_errors(self):
        self.gemini_key()
        agent = self.agent("gemini", "gemini-2.5-pro")
        images = self.engine.images
        with self.gemini_router([]):
            with self.assertRaises(DomainError) as ctx:
                images.generate({"prompt": "x", "model": "gemini-nano-banana-2.1", "size": "512px"}, actor=agent, source="owner")
            self.assertIn("size", str(ctx.exception))
            self.providers.models.append({"provider": "gemini", "id": "gemini-3.1-flash-lite-image", "capabilities": ["image"]})
            with self.assertRaises(DomainError):
                images.generate({"prompt": "x", "model": "gemini-3.1-flash-lite-image", "size": "2K"}, actor=agent, source="owner")
            with self.assertRaises(DomainError):
                images.generate({"prompt": "x", "aspect_ratio": "7:3"}, actor=agent, source="owner")
            with self.assertRaises(DomainError):
                images.generate({"prompt": "x", "count": 5}, actor=agent, source="owner")
            with self.assertRaises(DomainError):
                images.generate({"prompt": "x", "format": "webp"}, actor=agent, source="owner")
        big = self.root / "big.png"
        big.write_bytes(png() + b"\0" * (8 * 1024 * 1024))
        choice = images.resolve(agent)
        with self.assertRaises(DomainError) as ctx:
            images.run_gemini(choice, "x", {"count": 1, "format": "png", "aspect_ratio": "1:1", "size": "1K"}, [Ref(big, "image/png", big.stat().st_size)])
        self.assertIn("7 MB", str(ctx.exception))
        too_many = [self.project()["id"] for _ in range(15)]
        with self.assertRaises(DomainError):
            images.generate({"prompt": "x", "reference_artifact_ids": too_many}, actor=agent, source="owner")

        def limited(req):
            raise http_error(429, "quota exhausted with key gem-key-123")
        with self.gemini_router([], interactions=limited):
            with self.assertRaises(DomainError) as ctx:
                images.generate({"prompt": "x"}, actor=agent, source="owner")
        self.assertEqual(ctx.exception.status, 429)
        self.assertNotIn("gem-key-123", str(ctx.exception))

        def blocked(req):
            return Resp({"promptFeedback": {"blockReason": "SAFETY"}})
        with self.gemini_router([], interactions=blocked):
            with self.assertRaises(DomainError) as ctx:
                images.generate({"prompt": "x"}, actor=agent, source="owner")
        self.assertIn("safety", str(ctx.exception))

    def test_gemini_partial_results_keep_the_good_images(self):
        self.gemini_key()
        agent = self.agent("gemini", "gemini-2.5-pro")
        state = {"n": 0}
        raw = base64.b64encode(png()).decode()

        def flaky(req):
            state["n"] += 1
            if state["n"] > 1:
                raise http_error(500, "internal")
            return Resp({"output_image": {"data": raw, "mime_type": "image/png"}})
        with self.gemini_router([], interactions=flaky):
            result = self.engine.images.generate({"prompt": "x", "count": 3}, actor=agent, source="owner")
        self.assertEqual(len(result["images"]), 1)
        self.assertIn("1 of 3", result["partial"])

    # ---- gateway ----------------------------------------------------------------------------------
    def test_gateway_request_mapping_and_responses(self):
        self.gateway()
        self.image_setting(mode="fixed", engine="gateway", model="gpt-image-1")
        raw = base64.b64encode(png()).decode()
        with patch.object(self.providers, "gateway", return_value={"data": [{"b64_json": raw}, {"url": "https://cdn.example/a.png"}]}) as call, \
                patch.object(self.engine.images, "download", return_value=png(4, 4)) as download:
            result = self.engine.images.generate({"prompt": "poster", "aspect_ratio": "2:3", "quality": "high", "format": "jpeg",
                                                  "count": 2, "background": "transparent", "style": "logo"}, source="owner")
        route, payload = call.call_args.args
        self.assertEqual(route, "/images/generations")
        self.assertEqual({k: payload[k] for k in ("model", "n", "size", "quality", "background", "output_format")},
                         {"model": "gpt-image-1", "n": 2, "size": "1024x1536", "quality": "high", "background": "transparent", "output_format": "jpeg"})
        self.assertIn("Minimalist vector logo mark", payload["prompt"])
        download.assert_called_once_with("https://cdn.example/a.png")
        self.assertEqual([(i["width"], i["height"]) for i in result["images"]], [(2, 2), (4, 4)])
        with patch.object(self.providers, "gateway", return_value={"data": [{"b64_json": raw}]}) as call:
            self.engine.images.generate({"prompt": "wide", "aspect_ratio": "3:2"}, source="owner")
            self.assertEqual(call.call_args.args[1]["size"], "1536x1024")
            self.assertNotIn("quality", call.call_args.args[1])
            self.assertNotIn("output_format", call.call_args.args[1])
            self.engine.images.generate({"prompt": "auto", "aspect_ratio": "auto"}, source="owner")
            self.assertEqual(call.call_args.args[1]["size"], "auto")
        with self.assertRaises(DomainError):
            self.engine.images.generate({"prompt": "x", "aspect_ratio": "16:9"}, source="owner")
        with patch.object(self.providers, "gateway", return_value={"data": [{"url": "http://insecure.example/a.png"}]}):
            with self.assertRaises(DomainError):
                self.engine.images.generate({"prompt": "x"}, source="owner")

    def test_download_accepts_only_public_https(self):
        for url in ("http://cdn.example/a.png", "https://user:pw@cdn.example/a.png", "https://cdn.example:8443/a.png"):
            with self.assertRaises(DomainError):
                self.engine.images.download(url)
        with patch("agentflow.images.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(DomainError) as ctx:
                self.engine.images.download("https://cdn.example/a.png")
            self.assertIn("private", str(ctx.exception))

    def test_legacy_gateway_setting_still_generates_artifacts(self):
        self.store.settings({"image_model": "legacy-image"})
        raw = base64.b64encode(png()).decode()
        project = self.project()
        with patch.object(self.providers, "gateway", return_value={"data": [{"b64_json": raw}]}) as call:
            result = self.engine.tool(self.store.get("agents", "a-lead"), {"name": "image.generate", "arguments": {"prompt": "QA"}},
                                      {"id": "r", "project_id": project["id"]})
        self.assertEqual(call.call_args.args[1]["model"], "legacy-image")
        self.assertTrue(self.store.get("artifacts", result["artifact_id"])["is_image"])

    # ---- presets, prompt --------------------------------------------------------------------------
    def test_style_presets_and_prompt_limits(self):
        self.assertEqual(list(STYLES), ["none", "photo", "product", "illustration", "3d", "flat-icon", "anime", "watercolor", "cinematic", "logo"])
        compose = ImageEngines.compose
        self.assertEqual(compose("cat", "none", ""), "cat")
        for style in list(STYLES)[1:]:
            self.assertIn(STYLES[style][1], compose("cat", style, ""))
            self.assertTrue(STYLES[style][1].isascii())
        self.assertTrue(compose("cat", "photo", "blur").endswith("Avoid: blur."))
        with self.assertRaises(DomainError):
            self.engine.images.generate({"prompt": "x" * 10001}, source="owner")
        with self.assertRaises(DomainError):
            self.engine.images.generate({"prompt": "  "}, source="owner")
        self.gateway()
        self.image_setting(mode="fixed", engine="gateway", model="gpt-image-1")
        with self.assertRaises(DomainError) as ctx:
            self.engine.images.generate({"prompt": "x", "style": "oil-painting"}, source="owner")
        self.assertIn("style", str(ctx.exception).lower())

    def test_image_helpers_read_formats(self):
        self.assertEqual(sniff(png())[0], "png")
        self.assertEqual(dimensions(png(5, 3)), (5, 3))
        self.assertEqual(sniff(b"\xff\xd8\xff\xe0" + b"\0" * 20)[0], "jpg")
        self.assertEqual(sniff(b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 20)[0], "webp")
        self.assertIsNone(sniff(b"GIF89a"))
        jpeg = b"\xff\xd8\xff\xe0\x00\x04\x00\x00\xff\xc0\x00\x0b\x08\x00\x20\x00\x40\x01\x01\x11\x00"
        self.assertEqual(dimensions(jpeg), (64, 32))

    # ---- settings ---------------------------------------------------------------------------------
    def test_settings_validation_merge_and_migration(self):
        settings = self.service.settings({"image": {"mode": "fixed", "engine": "codex", "model": "gpt-5.5"}})["image"]
        self.assertEqual((settings["mode"], settings["engine"], settings["model"]), ("fixed", "codex", "gpt-5.5"))
        self.assertEqual(settings["defaults"], DEFAULTS)
        merged = self.service.settings({"image": {"defaults": {"style": "photo", "count": 2}}})["image"]
        self.assertEqual((merged["defaults"]["style"], merged["defaults"]["count"], merged["defaults"]["size"]), ("photo", 2, "1K"))
        self.assertEqual(merged["engine"], "codex")                      # partial update keeps the rest
        agent = self.service.settings({"image": {"mode": "agent", "engine": None, "model": "",
                                                 "fallback": [{"engine": "gemini"}, {"engine": "gateway", "model": "gpt-image-1"}]}})["image"]
        self.assertEqual((agent["mode"], "engine" in agent, "model" in agent), ("agent", False, False))
        self.assertEqual(agent["fallback"], [{"engine": "gemini", "model": ""}, {"engine": "gateway", "model": "gpt-image-1"}])
        bad = [{"mode": "auto"}, {"mode": "fixed", "engine": None}, {"engine": "dalle"}, {"model": "bad model!"}, {"foo": 1},
               {"fallback": "gemini"}, {"fallback": [{"engine": "nope"}]}, {"fallback": [{"engine": "codex", "model": str(i)} for i in range(6)]},
               {"defaults": {"aspect_ratio": "7:3"}}, {"defaults": {"count": 9}}, {"defaults": {"count": True}},
               {"defaults": {"style": "x"}}, {"defaults": {"quality": "ultra"}}, {"defaults": {"format": "gif"}}, {"defaults": {"other": 1}}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(DomainError):
                self.service.settings({"image": value})
        with self.assertRaises(DomainError):
            self.service.settings({"image": "gateway"})
        # Legacy image_model -> fixed gateway, via the settings API and at startup.
        migrated = self.service.settings({"image_model": "old-model"})
        self.assertEqual((migrated["image"]["mode"], migrated["image"]["engine"], migrated["image"]["model"]), ("fixed", "gateway", "old-model"))
        data = self.store.settings()
        data.pop("image", None)
        data["image_model"] = "startup-model"
        with self.store.lock:
            self.store.conn.execute("UPDATE config SET value=? WHERE key='settings'", (json.dumps(data),))
        ImageEngines(self.engine)
        self.assertEqual(self.store.settings()["image"]["model"], "startup-model")
        self.store.settings({"image_model": "changed-later"})
        ImageEngines(self.engine)                                           # migrates once: an existing image setting wins
        self.assertEqual(self.store.settings()["image"]["model"], "startup-model")

    # ---- HTTP -------------------------------------------------------------------------------------
    def test_http_routes(self):
        self.gateway()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, "tok", 0))
        port = server.server_port
        server.RequestHandlerClass = make_handler(self.service, "tok", port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", port)
        headers = {"Cookie": "af_session=tok", "Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % port}
        try:
            conn.request("GET", "/api/images/engines", headers=headers)
            response = conn.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual([e["id"] for e in body["engines"]], ["codex", "gemini", "gateway"])
            self.assertIn("settings", body)
            conn.request("POST", "/api/settings", json.dumps({"image": {"mode": "fixed", "engine": "gateway", "model": "gpt-image-1"}}), headers)
            response = conn.getresponse()
            self.assertEqual(json.loads(response.read())["image"]["engine"], "gateway")
            raw = base64.b64encode(png()).decode()
            with patch.object(self.providers, "gateway", return_value={"data": [{"b64_json": raw}]}):
                conn.request("POST", "/api/images/generate", json.dumps({"prompt": "a mug"}), headers)
                response = conn.getresponse()
                body = json.loads(response.read())
            self.assertEqual(response.status, 201)
            self.assertEqual(body["engine"], "gateway")
            self.assertEqual(len(body["images"]), 1)
            self.assertTrue(self.store.get("artifacts", body["images"][0]["artifact_id"])["is_image"])
            conn.request("GET", body["url"], headers={"Cookie": "af_session=tok"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), png())
            conn.request("POST", "/api/images/generate", json.dumps({"prompt": "x", "engine": "gemini"}), headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 409)
            self.assertIn("Gemini API key", json.loads(response.read())["error"])
        finally:
            conn.close()
            server.shutdown()
            server.server_close()

    def test_tool_schema_is_registered_and_not_core(self):
        from agentflow.contract import CORE_TOOLS, TOOL_SCHEMAS
        self.assertNotIn("image.generate", CORE_TOOLS)
        for key in ("prompt", "aspect_ratio", "size", "quality", "count", "style", "negative", "reference_artifact_ids", "engine", "model"):
            self.assertIn(key, TOOL_SCHEMAS["image.generate"])


if __name__ == "__main__":
    unittest.main()
