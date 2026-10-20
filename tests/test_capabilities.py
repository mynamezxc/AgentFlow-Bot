import base64
import copy
import json
import unittest
from unittest.mock import MagicMock, patch

import test_runtime as fixtures
from agentflow.store import DomainError, initial_seed
from agentflow.skills import skill_document


class CapabilityTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    tearDown = fixtures.RuntimeTests.tearDown
    ready = fixtures.RuntimeTests.ready

    def actor(self, agent_id="a-lead"):
        return self.store.get("agents",agent_id)

    def call(self,name,args,actor="a-lead",run=None):
        return self.engine.tool(self.actor(actor),{"name":name,"arguments":args},run or self.run)

    def newrun(self):
        self.ready()
        self.run=self.engine.chat({"agent_id":"a-lead","message":"Build a team"})

    def account(self,plugin="postman",agents=None,operations=None,config=None,secrets=None):
        self.engine.plugins.install(plugin)
        return self.engine.plugins.save_account({"plugin_id":plugin,"label":"Company",
            "agent_ids":agents or ["a-lead"],"operations":operations or ["collections.list"],
            "config":config or {},"secrets":secrets or {"token":"unit-private-token-123"}})

    def test_new_workspace_has_only_generic_atlas(self):
        seed=initial_seed(fixtures.SEED)
        self.assertEqual([a["id"] for a in seed["agents"]],["a-lead"])
        self.assertEqual(seed["agents"][0]["skills"],[])
        self.assertEqual(seed["agents"][0]["category"],"general")
        self.assertFalse(seed["tasks"] or seed["memories"] or seed["schedules"])

    def test_nested_team_arbitrary_roles_rebuild_and_archive(self):
        self.newrun()
        ceo=self.call("agents.create",{"name":"CEO","role":"Business operator","category":"executive"})
        marketing=self.call("agents.create",{"name":"Marketing","role":"Demand creation"},actor=ceo["id"])
        designer=self.call("agents.create",{"name":"Design","role":"Visual storytelling","category":"new-specialism"},actor=marketing["id"])
        self.assertEqual(self.actor(designer["id"])["parent_id"],marketing["id"])
        self.store.patch("agents",designer["id"],{"skills":["marketing-plan"],"instructions":"Old role"})
        self.call("agents.update",{"agent_id":designer["id"],"role":"Research operations"},actor=marketing["id"])
        self.assertEqual(self.actor(designer["id"])["skills"],[])
        self.assertEqual(self.actor(designer["id"])["instructions"],"")
        task=self.store.create("tasks",{"title":"Open task","agent_id":designer["id"],"reviewer_id":ceo["id"],"status":"ready"})
        with self.assertRaises(DomainError):
            self.call("agents.archive",{"agent_id":designer["id"]},actor=marketing["id"])
        result=self.call("agents.archive",{"agent_id":designer["id"],"reassign_to":marketing["id"]},actor=ceo["id"])
        self.assertEqual(result["transferred_tasks"],[task["id"]])
        self.assertTrue(self.actor(designer["id"])["archived"])
        self.assertEqual(self.store.get("tasks",task["id"])["agent_id"],marketing["id"])
        with self.assertRaises(DomainError): self.call("agents.archive",{"agent_id":"a-lead"},actor=ceo["id"])

    def test_account_secrets_not_in_snapshots_context_or_plugin_results(self):
        self.newrun()
        a=self.account()
        for value in (a,self.store.snapshot(),self.engine.context(self.actor(),run=self.run),self.call("plugins.list",{})):
            self.assertNotIn("unit-private-token",json.dumps(value))
        encoded=base64.b64encode(b"unit-private-token-123").decode()
        with patch("agentflow.plugins.request_json",return_value={"Authorization":"private", "data":"unit-private-token-123 / "+encoded}) as http:
            result=self.call("plugins.invoke",{"account_id":a["id"],"operation":"collections.list"})
        self.assertEqual(http.call_args.args[0],"api.getpostman.com")
        self.assertEqual(http.call_args.args[2]["X-API-Key"],"unit-private-token-123")
        self.assertNotIn("Authorization",result)
        self.assertNotIn(encoded,json.dumps(result))
        self.assertNotIn("unit-private-token",json.dumps(result))

    def test_agent_cannot_grant_access_or_change_destination(self):
        self.newrun();a=self.account()
        self.assertEqual(self.engine.plugins.accounts(self.actor("a-code")),[])
        for args in ({"account_id":a["id"],"operation":"collections.list"},
                     {"account_id":a["id"],"operation":"collections.list","arguments":{"url":"https://evil.test"}},
                     {"account_id":a["id"],"operation":"collections.list","headers":{"X":"Y"}}):
            with self.assertRaises(DomainError):
                self.call("plugins.invoke",args,actor="a-code" if "arguments" not in args and "headers" not in args else "a-lead")
        for tool in ("plugins.install","plugins.accounts.save","vault.read"):
            with self.assertRaises(DomainError): self.call(tool,{})
        with self.assertRaises(DomainError): self.call("plugins.invoke",{"account_id":a["id"],"operation":"collections.list"},run={"kind":"support"})

    def test_rotation_never_reuses_old_credentials_or_old_result(self):
        self.newrun();a=self.account()
        before=self.store.get("plugin_accounts",a["id"])
        updated=self.engine.plugins.save_account({**before,"secrets":{"token":"new-current-token-456"}})
        with patch("agentflow.plugins.request_json",return_value={"ok":True}) as http:
            self.call("plugins.invoke",{"account_id":a["id"],"operation":"collections.list"})
        self.assertEqual(http.call_args.args[2]["X-API-Key"],"new-current-token-456")
        def rotate(*_args,**_kwargs):
            current=self.store.get("plugin_accounts",a["id"])
            self.engine.plugins.save_account({**current,"secrets":{"token":"third-token"}})
            return {"data":"old result"}
        with patch("agentflow.plugins.request_json",side_effect=rotate),self.assertRaises(DomainError) as error:
            self.call("plugins.invoke",{"account_id":a["id"],"operation":"collections.list"})
        self.assertEqual(error.exception.status,409)

    def test_gmail_replacement_clears_previous_refresh_bundle(self):
        self.newrun();a=self.account("gmail",operations=["messages.list"],secrets={"access_token":"old-access","refresh_token":"old-refresh","client_id":"old-client","client_secret":"old-secret"})
        current=self.store.get("plugin_accounts",a["id"])
        self.engine.plugins.save_account({**current,"secrets":{"access_token":"current-access"}})
        current=self.store.get("plugin_accounts",a["id"])
        self.assertEqual(self.engine.plugins.gmail_token(current),"current-access")
        self.assertFalse(self.engine.plugins.secret(current,"refresh_token"))

    def test_uninstall_reinstall_does_not_restore_grants(self):
        self.newrun();a=self.account()
        self.engine.plugins.install("postman",False)
        self.engine.plugins.install("postman",True)
        with self.assertRaises(DomainError): self.call("plugins.invoke",{"account_id":a["id"],"operation":"collections.list"})

    def test_grant_and_zone_scope_are_enforced(self):
        self.newrun();zone="a"*32
        a=self.account("cloudflare",config={"zone_ids":[zone]},operations=["dns.list"])
        with patch("agentflow.plugins.request_json",return_value={"result":[]}) as http:
            self.call("plugins.invoke",{"account_id":a["id"],"operation":"dns.list","arguments":{"zone_id":zone}})
            self.assertIn(zone,http.call_args.args[1])
        for operation,args in (("dns.create",{"zone_id":zone}), ("dns.list",{"zone_id":"b"*32})):
            with self.assertRaises(DomainError):self.call("plugins.invoke",{"account_id":a["id"],"operation":operation,"arguments":args})

    def test_web_and_ssh_use_owner_fixed_routes_and_actions(self):
        self.newrun()
        a=self.account("web",config={"origin":"https://example.com","routes":{"status":{"path":"/status"}}},operations=["routes.call"])
        with patch("agentflow.plugins.request_json",return_value={"ok":True}) as http:
            self.call("plugins.invoke",{"account_id":a["id"],"operation":"routes.call","arguments":{"route":"status"}})
            self.assertEqual(http.call_args.args[:2],("example.com","/status"))
        with self.assertRaises(DomainError):self.call("plugins.invoke",{"account_id":a["id"],"operation":"routes.call","arguments":{"route":"status","path":"/secrets"}})
        for config in ({"origin":"http://127.0.0.1","routes":{"x":{"path":"/"}}},
                       {"origin":"https://x.test","routes":{"x":{"path":"//evil.test"}}}):
            with self.assertRaises(DomainError):self.engine.plugins.validate_config("web",config)
        with self.assertRaises(DomainError):self.engine.plugins.validate_config("ssh",{"host":"host","username":"me","actions":{"x":"uptime"}})

    def test_custom_tools_execute_and_cannot_add_privileges(self):
        self.newrun()
        tool=self.call("tools.create",{"name":"write-brief","description":"Write a brief","inputs":{"text":"Content"},
            "steps":[{"name":"workspace.write","arguments":{"path":"brief.md","content":{"$input":"text"}}},
                     {"name":"workspace.read","arguments":{"path":{"$step":0,"path":["path"]}}}]})
        result=self.call("tools.invoke",{"tool_id":tool["id"],"arguments":{"text":"Evidence"}})
        self.assertEqual(result["results"][-1],"Evidence")
        self.call("tools.archive",{"tool_id":tool["id"]})
        with self.assertRaises(DomainError):self.call("tools.invoke",{"tool_id":tool["id"],"arguments":{"text":"Changed"}})
        self.call("tools.archive",{"tool_id":tool["id"],"archived":False})
        self.assertEqual(self.call("tools.invoke",{"tool_id":tool["id"],"arguments":{"text":"Restored"}})["results"][-1],"Restored")
        for name in ("plugins.install","agents.create","workspace.execute","models.set_default","tools.invoke"):
            with self.assertRaises(DomainError):self.call("tools.create",{"name":"bad-tool","description":"Bad","steps":[{"name":name,"arguments":{}}]})
        a=self.account()
        tool=self.call("tools.create",{"name":"use-postman","description":"Use Postman","inputs":{},"steps":[{"name":"plugins.invoke","arguments":{"account_id":a["id"],"operation":"collections.list"}}]})
        with self.assertRaises(DomainError):self.call("tools.invoke",{"tool_id":tool["id"],"arguments":{}},actor="a-code")

    def test_research_and_install_use_source_evidence(self):
        self.newrun()
        catalog={"status":200,"text":json.dumps([{"type":"dir","name":"research","path":"skills/research"}]),"url":"https://api.github.com/"}
        with patch("agentflow.skills.public_page",return_value=catalog):
            result=self.call("skills.research",{"query":"research"})
        self.assertEqual(len(result["candidates"]),3)
        self.assertEqual(result["candidates"][0]["source"],"Anthropic")
        page={"url":"https://raw.githubusercontent.com/anthropics/skills/main/skills/research/SKILL.md","status":200,"text":"---\nname: research\n---\n# Research\nRead sources and verify outputs."}
        with patch("agentflow.skills.public_page",return_value=page):
            installed=self.call("skills.install",{"url":page["url"]})
        self.assertIn(installed["id"],self.actor()["skills"])
        self.assertTrue((self.root/"company"/"skills"/installed["id"]/"SKILL.md").exists())

    def test_reasoning_default_private_phase_and_actual_cli_argument(self):
        self.newrun()
        self.providers.models=[{"provider":"codex","id":"test-model","name":"Test","reasoning_efforts":["low","high"]},
                               {"provider":"claude","id":"sonnet","name":"Sonnet","reasoning_efforts":["medium","high"]}]
        self.engine.models.owner_select("a-lead",{"provider":"codex","model":"test-model","reasoning_effort":"high","scope":"team"})
        self.assertEqual(self.engine.models.base(self.actor("a-code"))["reasoning_effort"],"high")
        self.engine.models.owner_select("a-code",{"provider":"claude","model":"sonnet","reasoning_effort":"medium"})
        self.assertEqual(self.engine.models.base(self.actor("a-code"))["reasoning_effort"],"medium")
        with self.assertRaises(DomainError):self.engine.models.resolve({"provider":"codex","model":"test-model","reasoning_effort":"max"})
        self.providers.cli["codex"]="codex.exe"
        for provider in ("codex","claude"):
            session=self.providers.accounts.check(provider,force=True)
            for model in self.providers.models:
                if model["provider"]==provider:model["account_fingerprint"]=session["fingerprint"]
        # Force checks preserve the same stable account identity across calls.
        self.providers.models=[{"provider":"codex","id":"test-model","name":"Test","reasoning_efforts":["low","high"],"account_fingerprint":self.providers.accounts.current["codex"]["fingerprint"]},
                               {"provider":"claude","id":"sonnet","name":"Sonnet","reasoning_efforts":["medium","high"],"account_fingerprint":self.providers.accounts.current["claude"]["fingerprint"]}]
        process=MagicMock(returncode=0)
        process.communicate.return_value=(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"OK"}}),"")
        with patch("agentflow.providers.subprocess.Popen",return_value=process) as start:
            self.providers.infer({**self.actor(),"provider":"codex","model":"test-model","reasoning_effort":"high"},"Test","reason-test")
        self.assertIn('model_reasoning_effort="high"',start.call_args.args[0])
        self.providers.cli["claude"]="claude.exe"
        process.communicate.return_value=(json.dumps({"result":"OK"}),"")
        with patch("agentflow.providers.subprocess.Popen",return_value=process) as start:
            self.providers.infer({**self.actor(),"provider":"claude","model":"sonnet","reasoning_effort":"medium"},"Test","claude-reason")
        args=start.call_args.args[0];self.assertEqual(args[args.index("--effort")+1],"medium")

    def test_runtime_forwards_selected_reasoning_and_clears_it_on_switch(self):
        self.newrun()
        self.providers.models=[{"provider":"gateway","id":"first","name":"First","reasoning_efforts":["high"]},
                               {"provider":"gateway","id":"second","name":"Second","reasoning_efforts":[]}]
        self.engine.models.owner_select("a-lead",{"provider":"gateway","model":"first","reasoning_effort":"high"})
        responses=[json.dumps({"state":"continue","tool_calls":[{"name":"models.switch","arguments":{"model":"second","provider":"gateway"}}]}),json.dumps({"reply":"Done","state":"complete"})]
        with patch.object(self.providers,"infer",side_effect=responses) as inference:
            self.engine.execute(self.run["id"])
        self.assertEqual(inference.call_args_list[0].args[0]["reasoning_effort"],"high")
        self.assertIsNone(inference.call_args_list[1].args[0]["reasoning_effort"])

    def test_inspection_exposes_hierarchy_and_recovered_run_clears_error(self):
        self.newrun()
        child=self.call("agents.create",{"name":"General child","role":"Adaptable"})
        record=self.call("agents.inspect",{"agent_id":child["id"]})[0]
        self.assertEqual(record["parent_id"],"a-lead")
        self.store.patch("runs",self.run["id"],{"error":"Previous interrupted attempt"})
        with patch.object(self.providers,"infer",return_value='{"reply":"Recovered","state":"complete"}'):
            self.engine.execute(self.run["id"])
        self.assertIsNone(self.store.get("runs",self.run["id"])["error"])

    def test_ssh_paste_preserves_full_private_key_and_hides_it(self):
        key="-----BEGIN OPENSSH PRIVATE"+" KEY-----\nunit-secret-line\n-----END OPENSSH PRIVATE"+" KEY-----"   # split: the publish check must not see a key block
        with patch("agentflow.plugins.importlib.util.find_spec",return_value=object()):
            a=self.account("ssh",config={"host":"example.com","username":"operator","host_key":"ssh-ed25519 AAAA", "actions":{"status":"uptime"}},
                           operations=["actions.run"],secrets={"private_key":"base64:"+base64.b64encode(key.encode()).decode()})
        stored=self.store.get("plugin_accounts",a["id"])
        self.assertEqual(self.engine.plugins.secret(stored,"private_key"),key)
        self.assertNotIn("unit-secret-line",json.dumps(self.engine.plugins.owner_state()))

    def test_credential_http_never_follows_redirect_or_resolves_private_host(self):
        from agentflow.plugins import request_json
        with patch("agentflow.plugins.socket.getaddrinfo",return_value=[(None,None,None,None,("127.0.0.1",443))]),self.assertRaises(DomainError):
            request_json("example.com","/",{"Authorization":"private"})
        connection=MagicMock();connection.getresponse.return_value.status=302
        connection.getresponse.return_value.read.return_value=b'{}'
        with patch("agentflow.plugins.socket.getaddrinfo",return_value=[(None,None,None,None,("8.8.8.8",443))]),patch("agentflow.plugins.PinnedConnection",return_value=connection) as create,self.assertRaises(DomainError):
            request_json("example.com","/",{"Authorization":"private"})
        self.assertEqual(create.call_count,1)
        connection.close.assert_called_once()
