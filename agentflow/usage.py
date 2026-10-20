"""Observed subscription quota windows, always bound to the current native account.

Design rules (see the usage-accuracy sprint):
* A window set belongs to an identity fingerprint, never to credential file timestamps. While the
  fingerprint is unchanged the last confirmed windows stay visible (``updating`` / ``stale`` flags
  describe freshness). Only a different confirmed identity clears them.
* A failed or throttled refresh (HTTP 401/429, timeout, empty answer) never raises into the UI and
  never erases data; it only sets a notice.
* Windows whose reset time has passed are reported as ``expired`` with ``used_percent`` 0 until the
  next successful refresh replaces them.
"""
import hashlib
import json
import math
import threading
import time
import urllib.request
import urllib.error
import weakref
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .accounts import AccountChanged, identity_digest
from .store import DomainError, now

STALE_AFTER = 300   # quota gating trusts a measurement for five minutes
AGED_AFTER = 900    # the UI draws a value older than 15 minutes as stale (never hides it)
EVENT_FRESH = 300   # a rate_limit_event this recent already carries fresh numbers
SAME_WINDOW = 600   # reset times within 10 minutes of each other describe the same window
REFRESH_DEBOUNCE = 20
MIN_ATTEMPT_GAP = 15
CLAUDE_TOKEN_NOTICE = 'Claude token refreshes on next Claude run; showing last confirmed usage.'
CLAUDE_USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
CLAUDE_RATE_NOTICE = 'Claude usage endpoint rate-limited; showing last confirmed usage.'
CLAUDE_CHECKING_NOTICE = 'Checking the current Claude account; showing last confirmed usage.'
BACKOFF_MIN, BACKOFF_MAX = 300, 3600   # 429 from the usage endpoint: 5 -> 10 -> 20 -> 40 min, at most 60 min
RETRY_AFTER_MIN = 5
EVENT_KINDS = {'session': 'five_hour', 'five_hour': 'five_hour', 'weekly': 'seven_day', 'week': 'seven_day', 'seven_day': 'seven_day',
    'weekly_opus': 'seven_day_opus', 'weekly_sonnet': 'seven_day_sonnet'}
CREDENTIAL_REFRESH_DELAY = 2.0        # Claude Code rewrote its credentials: read usage again shortly
CREDENTIAL_POLL = 2.0


def epoch(value):
    """Seconds since the epoch from seconds, milliseconds or an ISO-8601 string; else None."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return value / 1000 if value > 1e11 else value


def window(key, percent, reset, minutes=None, label=None, status=None):
    if isinstance(percent,bool) or percent is not None and (not isinstance(percent,(float,int)) or not math.isfinite(percent) or percent < 0):
        percent=None
    reset=epoch(reset)
    if isinstance(minutes,bool) or not isinstance(minutes,(int,float)) or not math.isfinite(minutes) or minutes<=0:
        minutes=None
    return {'id':key,'label':label or key,'used_percent':percent,'resets_at':reset,'window_minutes':minutes,'status':status}


def pick(mapping, *names):
    for name in names:
        if isinstance(mapping, dict) and mapping.get(name) is not None:
            return mapping[name]
    return None


def parse_retry_after(value, current=None):
    """Seconds to wait from a Retry-After header (delta-seconds or an HTTP date); None when absent or unreadable."""
    if value is None:return None
    text=str(value).strip()
    if not text:return None
    try:
        seconds=float(text)
        return seconds if math.isfinite(seconds) and seconds>=0 else None
    except ValueError:pass
    try:
        when=parsedate_to_datetime(text)
        if when.tzinfo is None:when=when.replace(tzinfo=timezone.utc)
        return max(0.0,when.timestamp()-(time.time() if current is None else current))
    except (TypeError,ValueError,IndexError):return None


def claude_minutes(key):
    return 300 if key in ('five_hour', 'session') else 10080 if str(key).startswith('seven_day') else None


class Usage:
    def __init__(self, providers):
        self.providers=providers
        self.lock=threading.RLock()
        self.path=providers.store.directory/'provider-usage.json'
        try:
            self.cache=json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.cache,dict):self.cache={}
        except (OSError,ValueError):self.cache={}
        self.attempts={}
        self.errors={}
        self.backoff={}
        self.backoff_count={}
        self.event_at={}
        self.restore_backoff()
        self.auth_hold={}
        self.pending_inference={}
        self.refresh_requested={}
        self._credential_signature=self.credentials_signature()
        self._watcher=None

    def restore_backoff(self):
        """Rate-limit state survives a restart: the endpoint is not asked again before the stored time."""
        saved=(self.cache.get('_backoff') or {}).get('claude') if isinstance(self.cache.get('_backoff'),dict) else None
        if not isinstance(saved,dict):return
        try:
            count=int(saved.get('count') or 0)
            remaining=float(saved.get('until') or 0)-time.time()
        except (TypeError,ValueError):return
        if count>0:self.backoff_count['claude']=count
        if remaining>0:self.backoff['claude']=time.monotonic()+min(remaining,BACKOFF_MAX)

    def remember_backoff(self,provider='claude'):
        with self.lock:
            remaining=self.backoff.get(provider,0)-time.monotonic()
            count=self.backoff_count.get(provider,0)
            book=self.cache.get('_backoff') if isinstance(self.cache.get('_backoff'),dict) else {}
            if remaining>0 or count:book[provider]={'until':time.time()+max(0,remaining),'count':count}
            else:book.pop(provider,None)
            if book:self.cache['_backoff']=book
            else:self.cache.pop('_backoff',None)
            self.persist()

    def persist(self):
        try:
            tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(self.cache),encoding='utf-8');tmp.replace(self.path)
        except OSError:
            pass  # The in-memory observation is still valid; persistence is best effort.

    def same_identity(self,provider,old,fingerprint,metadata):
        if old.get('account_fingerprint')==fingerprint:return True
        stored,given=old.get('identity_key'),metadata.get('identity_key')
        if stored and given:return stored==given
        # Claude: a cache without identity info (older build) or an identity file that cannot be read right now
        # (the inference already ran under the signed-in account) is accepted; public() still checks the fingerprint.
        return provider=='claude' and bool(old.get('windows')) and not (stored and given)

    def save(self,provider,fingerprint,windows,source,merge=False,**metadata):
        with self.lock:
            old=self.cache.get(provider,{})
            observed,epoch_now=now(),time.time()
            windows=[{**w,'observed_at':observed,'observed_epoch':epoch_now,'retained':False,'percent_epoch':w.get('percent_epoch') or epoch_now} for w in windows]
            if provider=='claude' and 'identity_key' not in metadata:
                metadata['identity_key']=self.providers.accounts.claude_identity_key()
            if merge and self.same_identity(provider,old,fingerprint,metadata):
                ids={w['id'] for w in windows}
                windows=[{**w,'retained':True,'observed_at':w.get('observed_at',old.get('observed_at')),'observed_epoch':w.get('observed_epoch',old.get('observed_epoch',0))} for w in old.get('windows',[]) if w['id'] not in ids]+windows
            self.cache[provider]={'account_fingerprint':fingerprint,'windows':windows,'source':source,'observed_at':observed,'observed_epoch':epoch_now,**metadata}
            self.errors.pop(provider,None)
            self.persist()

    def note(self,provider,message):
        """Record why the latest refresh produced nothing; confirmed data stays visible."""
        with self.lock:
            self.errors[provider]=message
        return False

    def note_inference(self,provider):
        """Call after an inference on `provider` finishes: usage changed, refresh soon (debounced)."""
        if provider not in ('codex','claude'):return
        with self.lock:
            self.pending_inference[provider]=time.monotonic()
        kick=getattr(self.providers,'kick_usage',None)
        if kick:
            try:kick(provider)
            except Exception:pass

    def has_expired(self,provider):
        """True when a stored window reset after it was observed, so a fresh reading is due."""
        with self.lock:
            data=self.cache.get(provider,{})
            current=time.time()
            return any(w.get('resets_at') and w['resets_at']<=current and w.get('observed_epoch',data.get('observed_epoch',0))<w['resets_at']
                for w in data.get('windows',[]))

    def event_fresh(self,provider):
        """True when a rate_limit_event updated the windows within the last five minutes."""
        return time.monotonic()-self.event_at.get(provider,-1e9)<EVENT_FRESH

    def throttled(self,provider,fingerprint,force):
        with self.lock:
            key=(provider,fingerprint)
            if not force and time.monotonic()-self.attempts.get(key,0)<MIN_ATTEMPT_GAP:return True
            self.attempts[key]=time.monotonic()
            return False

    # ---- Codex -------------------------------------------------------------------------------
    def refresh_codex(self,state,force=False):
        if self.throttled('codex',state['fingerprint'],force):return False
        result=self.providers._codex_models(include_models=False,read_usage=True)
        return self.apply_codex(state,result)

    def apply_codex(self,state,result):
        """Store a `_codex_models(read_usage=True)` result for the identity in `state`."""
        fingerprint=state['fingerprint']
        result=result if isinstance(result,dict) else {}
        accounts=self.providers.accounts
        if 'account' in result and identity_digest(result['account'])!=state.get('identity_digest'):
            # The digest ignores plan/token fields, so a mismatch means a real identity change or a
            # state saved by an older build. Re-confirm with the account we just read instead of
            # dropping the observation; only a different fingerprint discards it.
            if accounts.check('codex',force=True,account=result['account'])['fingerprint']!=fingerprint:
                raise AccountChanged()
        accounts.assert_current('codex',fingerprint,force=False)
        limits=result.get('limits') or {}
        if not isinstance(limits,dict):raise DomainError('Codex returned invalid quota metadata.',502)
        legacy=pick(limits,'rateLimits','rate_limits') or {}
        legacy=legacy if isinstance(legacy,dict) else {}
        default_bucket=pick(legacy,'limitId','limit_id') or 'codex'
        buckets=pick(limits,'rateLimitsByLimitId','rate_limits_by_limit_id') or {}
        if not isinstance(buckets,dict):raise DomainError('Codex returned invalid quota buckets.',502)
        buckets=dict(buckets)
        if legacy and default_bucket not in buckets:buckets[default_bucket]=legacy
        windows=[]
        for key,bucket in buckets.items():
            if not isinstance(bucket,dict):continue
            for name in ('primary','secondary'):
                w=bucket.get(name)
                if not isinstance(w,dict):continue
                percent=pick(w,'usedPercent','used_percent')
                if percent is None:continue
                reset=pick(w,'resetsAt','resets_at')
                if reset is None:
                    after=pick(w,'resetsInSeconds','resets_in_seconds','resetAfterSeconds')
                    reset=time.time()+after if isinstance(after,(int,float)) and not isinstance(after,bool) and after>0 else None
                windows.append({**window(key+':'+name,percent,reset,pick(w,'windowDurationMins','window_duration_mins','windowMinutes'),
                    pick(bucket,'limitName','limit_name') or key),'bucket':key})
        plan=next((p for p in [pick(legacy,'planType','plan_type')]+[pick(b,'planType','plan_type') for b in buckets.values() if isinstance(b,dict)]
            +[state.get('plan_type'),pick(result.get('account') if isinstance(result.get('account'),dict) else {},'planType')] if p),None)
        # An empty transient response is not a new zero-usage observation.
        if windows:
            self.save('codex',fingerprint,windows,'Codex account/rateLimits/read',merge=True,default_bucket=default_bucket,plan_type=plan)
            return True
        return self.note('codex','Codex returned no quota windows. Showing the last confirmed observation.' if not result.get('limits_error')
            else 'Codex could not read usage right now. Showing the last confirmed observation.')

    # ---- Claude ------------------------------------------------------------------------------
    def claude_token(self):
        """(accessToken, expiresAt seconds) from the native store. Tokens never leave this broker."""
        path=self.providers.accounts.home('claude')/'.credentials.json'
        for attempt in (0,1):
            try:
                oauth=json.loads(path.read_text(encoding='utf-8')).get('claudeAiOauth') or {}
                expires=oauth.get('expiresAt')
                return oauth.get('accessToken'),epoch(expires) if isinstance(expires,(int,float)) else None
            except FileNotFoundError:
                break
            except (OSError,ValueError,AttributeError):
                if attempt==0:time.sleep(0.2)  # Claude Code may be rewriting the file right now.
        return None,None

    def credentials_signature(self):
        """Digest of the Claude credential file's current content (None when absent). Never exposes the token."""
        try:
            return hashlib.sha256((self.providers.accounts.home('claude')/'.credentials.json').read_bytes()).hexdigest()
        except OSError:
            return None

    def poll_credentials(self,kick=False):
        """Detect that Claude Code rewrote its credentials (token refreshed / re-login) and schedule a usage read ~2 s later."""
        signature=self.credentials_signature()
        with self.lock:
            changed=signature!=self._credential_signature
            self._credential_signature=signature
            if changed and signature is not None:
                self.refresh_requested['claude']=time.monotonic()+CREDENTIAL_REFRESH_DELAY
            else:changed=False
        if changed and kick:
            try:self.providers._schedule('claude',polled=time.monotonic()-self.providers.last_poll<self.providers.POLL_WINDOW)
            except Exception:pass
        return changed

    def ensure_watch(self):
        """Poll the credential file every couple of seconds while Claude is installed (cheap: one small read)."""
        with self.lock:
            if self._watcher and self._watcher.is_alive():return
            if not self.providers.cli.get('claude'):return
            reference=weakref.ref(self)
            def watch():
                while True:
                    time.sleep(CREDENTIAL_POLL)
                    usage=reference()
                    if usage is None:return
                    try:usage.poll_credentials(kick=True)
                    except Exception:pass
                    del usage
            self._watcher=threading.Thread(target=watch,daemon=True,name='claude-credentials-watch')
            self._watcher.start()

    def refresh_claude(self,state,force=False):
        """Read the subscription endpoint; credentials never leave this broker.

        This is a vendor-internal endpoint and may be unavailable for some plans. Do not run
        inference or estimate quota from token counts as a fallback. Every failure keeps the last
        confirmed windows and only sets a notice. 429 backs off exponentially (2 -> 15 min) even for
        forced refreshes; a 401 is not retried until the credential file changes.
        """
        fingerprint=state['fingerprint']
        if time.monotonic()<self.backoff.get('claude',0):return False  # the 429 notice stays visible
        if self.throttled('claude',fingerprint,force):return False
        self.providers.accounts.assert_current('claude',fingerprint,force=False)
        token,expires=self.claude_token()
        if not token:
            return self.note('claude','Claude subscription usage is unavailable from this native credential store. Usage events will be captured during normal work.')
        signature=self.credentials_signature()
        if self.auth_hold.get('claude') not in (None,signature):self.auth_hold.pop('claude',None)
        if self.auth_hold.get('claude'):return self.note('claude',CLAUDE_TOKEN_NOTICE)  # same rejected token: wait for new credentials
        if expires and expires<=time.time()+30:
            return self.note('claude',CLAUDE_TOKEN_NOTICE)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        request=urllib.request.Request(CLAUDE_USAGE_URL,headers={
            'Authorization':'Bearer '+token,'anthropic-beta':'oauth-2025-04-20','Accept':'application/json'})
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=10) as response:
                payload=json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401,403):
                self.auth_hold['claude']=signature
                return self.note('claude',CLAUDE_TOKEN_NOTICE)
            if exc.code==429:
                # Rate limiting is not an error for the owner: the last windows stay as they are, the popover shows the next check.
                count=self.backoff_count.get('claude',0)+1
                self.backoff_count['claude']=count
                delay=min(BACKOFF_MAX,BACKOFF_MIN*2**(count-1))
                try:retry=parse_retry_after((getattr(exc,'headers',None) or {}).get('Retry-After'))
                except AttributeError:retry=None
                if retry is not None:delay=min(BACKOFF_MAX,max(RETRY_AFTER_MIN,retry))
                self.backoff['claude']=time.monotonic()+delay
                self.remember_backoff('claude')
                return False
            return self.note('claude','Claude subscription usage could not be refreshed (HTTP '+str(exc.code)+'). Showing last confirmed usage.')
        except (urllib.error.URLError,OSError,ValueError):
            return self.note('claude','Claude subscription usage could not be refreshed. Showing last confirmed usage; no model request was made.')
        self.providers.accounts.assert_current('claude',fingerprint,force=False)
        if not isinstance(payload,dict):return self.note('claude','Claude returned invalid usage metadata. Showing last confirmed usage.')
        windows=[]
        for key,entry in payload.items():
            # `utilization` is already 0-100 here (rate_limit_event reports a 0-1 fraction).
            if not isinstance(entry,dict) or not isinstance(entry.get('utilization'),(int,float)) or isinstance(entry.get('utilization'),bool):continue
            windows.append(window(key,entry.get('utilization'),entry.get('resets_at'),claude_minutes(key)))
        if windows:
            # A reply without the 5-hour window is partial: keep the windows it did not mention.
            self.save('claude',fingerprint,windows,'Claude subscription usage',merge=not any(w['id']=='five_hour' for w in windows),plan_type=state.get('plan_type'))
            self.backoff.pop('claude',None)
            self.backoff_count.pop('claude',None)
            self.auth_hold.pop('claude',None)
            self.remember_backoff('claude')
            return True
        return self.note('claude','Claude returned no subscription quota windows. Waiting for native usage events.')

    def claude_events(self,records,state):
        """Fold `rate_limit_event` records from an inference into the stored windows (never raises, never spawns)."""
        windows={}
        with self.lock:
            previous=self.cache.get('claude',{})
            known={w['id']:w for w in previous.get('windows',[])} if previous.get('account_fingerprint')==state['fingerprint'] else {}
        for record in records:
            if record.get('type')!='rate_limit_event':continue
            info=record.get('rate_limit_info') or {}
            raw=info.get('rateLimitType') or 'session'
            kind=EVENT_KINDS.get(raw,raw)   # the ids the subscription endpoint uses, so events merge instead of duplicating
            utilization=info.get('utilization')
            # Events carry a 0-1 fraction, the subscription endpoint a 0-100 percentage.
            percent=round(utilization*100,2) if isinstance(utilization,(int,float)) and not isinstance(utilization,bool) else None
            if percent is None and info.get('status')=='rejected':percent=100
            reset=epoch(info.get('resetsAt'))
            before=windows.get(kind,known.get(kind)) or {}
            estimated=False
            measured=None   # when the percentage was really measured (carried values keep their own time)
            if percent is None and before.get('used_percent') is not None:
                # An event without utilization never erases a known percentage.
                earlier=before.get('resets_at')
                percent=before['used_percent']
                measured=before.get('percent_epoch') or before.get('observed_epoch')
                estimated=bool(before.get('estimated'))
                if reset is None or earlier is None or abs(reset-earlier)<=SAME_WINDOW:
                    reset=reset or earlier   # the same window: only the status and the observation time move
                elif reset>earlier:
                    if earlier<=time.time():percent,measured,estimated=0,None,False   # the old window ended: a new one starts empty
                    else:estimated=True   # a new window while the old one is still open: keep the last value, marked as an estimate
                else:estimated=True
            item=window(kind,percent,reset,claude_minutes(kind),kind,info.get('status'))
            if estimated:item['estimated']=True
            if measured:item['percent_epoch']=measured
            windows[kind]=item
        if windows:
            # The inference already ran under this identity; only skip when the cached identity is known to differ.
            current=self.providers.accounts.current.get('claude')
            if not current or current.get('fingerprint')==state['fingerprint']:
                self.save('claude',state['fingerprint'],list(windows.values()),'Claude Code rate_limit_event',merge=True)
                self.event_at['claude']=time.monotonic()
        self.note_inference('claude')

    # ---- Public view -------------------------------------------------------------------------
    def bridge(self,data,state,revision_changed):
        """Notice when the stored Claude windows still belong to the signed-in identity although the
        fingerprint/revision is momentarily unconfirmed (auth status timeout, credential rewrite); else None."""
        if not data.get('windows'):return None
        if state and state.get('signed_in') is False and not revision_changed:return None  # confirmed signed out
        accounts=self.providers.accounts
        local=accounts.claude_identity_key()
        stored=data.get('identity_key')
        if stored and local:
            return CLAUDE_CHECKING_NOTICE if stored==local else None
        if stored and not local:return CLAUDE_CHECKING_NOTICE  # identity file unreadable right now: keep what we know
        # Cache written before identity keys existed: trust the last confirmed fingerprint of this machine.
        reference=state or accounts.persisted.get('claude') or {}
        if reference.get('fingerprint') and reference['fingerprint']==data.get('account_fingerprint'):
            if local:data['identity_key']=local
            return CLAUDE_CHECKING_NOTICE
        return None

    def bridge_persisted(self,provider,data,state):
        """Right after a restart nothing is confirmed yet: windows saved for the fingerprint the last run confirmed stay visible."""
        if state or not data.get('windows'):return None
        saved=self.providers.accounts.persisted.get(provider) or {}
        if saved.get('fingerprint') and saved['fingerprint']==data.get('account_fingerprint'):
            return 'Checking the current %s account; showing last confirmed usage.'%provider.capitalize()
        return None

    def public(self,provider,pending=False):
        with self.lock:
            accounts=self.providers.accounts
            state=accounts.current.get(provider,{})
            data=self.cache.get(provider,{})
            revision_changed=accounts.revisions.get(provider)!=accounts.revision(provider)
            checking=None
            if revision_changed or not state.get('signed_in') or data.get('account_fingerprint')!=state.get('fingerprint'):
                checking=self.bridge(data,state,revision_changed) if provider=='claude' else self.bridge_persisted(provider,data,state)
                if not checking:
                    # Identity differs (or is unconfirmed after a real change): never show another account's data.
                    signed_out=bool(state) and not state.get('signed_in') and not revision_changed
                    return {'windows':[],'state':'pending' if pending else 'unavailable','updating':bool(pending),'stale':False,
                        'notice':'Not signed in.' if signed_out and not pending else 'Checking the current account.' if pending else 'No quota data for the current account.'}
            current=time.time()
            windows=[]
            for w in data.get('windows',[]):
                expired=bool(w.get('resets_at') and w['resets_at']<=current)
                measured=w.get('percent_epoch') or w.get('observed_epoch',data.get('observed_epoch',0))
                item={**w,'expired':expired,
                    'stale':bool(w.get('retained')) or bool(w.get('estimated')) or current-measured>STALE_AFTER,
                    'aged':bool(w.get('retained')) or bool(w.get('estimated')) or current-measured>AGED_AFTER}
                if expired:
                    # The window rolled over: usage is back to zero until the refresh reports the new window.
                    item.update(used_percent=0 if w.get('used_percent') is not None else None,previous_used_percent=w.get('used_percent'))
                windows.append(item)
            errors=getattr(self,'errors',{})
            account_errors=getattr(self.providers,'account_errors',{})
            age=current-data.get('observed_epoch',0)
            wait=self.backoff.get(provider,0)-time.monotonic()
            limited=wait>0
            return {'rate_limited':limited,'next_check_epoch':current+wait if limited else None,
                'rate_notice':CLAUDE_RATE_NOTICE if limited else '','windows':windows,'state':'observed' if windows else 'unavailable','source':data.get('source'),
                'default_bucket':data.get('default_bucket','codex'),'plan_type':data.get('plan_type') or state.get('plan_type'),
                'observed_at':data.get('observed_at'),'observed_epoch':data.get('observed_epoch'),
                'stale':age>AGED_AFTER or bool(errors.get(provider)) or provider in account_errors or bool(checking),'updating':bool(pending) or bool(checking),
                'expired':any(w['expired'] for w in windows),
                'notice':('Account could not be rechecked. Showing the last confirmed observation.' if provider in account_errors else errors.get(provider) or checking) or ''}
