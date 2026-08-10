from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from rnaseq_agent.gui import ConfigApp
from rnaseq_agent.llm import LLMDecision, OnboardingDecision
from rnaseq_agent.llm_policy import ConfigPatch, OnboardingPatch
from rnaseq_agent.reference_catalog import candidates_for_species


class GuiLLMSafetyTests(unittest.TestCase):
    def test_footer_separates_status_from_two_action_rows(self) -> None:
        source = inspect.getsource(ConfigApp._build_ui)

        self.assertIn('status_row = ttk.Frame(footer', source)
        self.assertIn('action_row = ttk.Frame(footer', source)
        self.assertIn('row, column = divmod(index, 4)', source)
        self.assertIn('.grid(', source)
        self.assertIn('for column in range(4):', source)

    def test_send_chat_message_is_a_config_app_callback(self) -> None:
        self.assertTrue(callable(getattr(ConfigApp, "send_chat_message", None)))

    def test_send_chat_message_handles_local_help_without_model(self) -> None:
        class ChatInput:
            def __init__(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        app = object.__new__(ConfigApp)
        app.chat_input = ChatInput("帮助")
        app.awaiting_species_answer = False
        app.onboarding_active = False
        messages: list[tuple[str, str]] = []
        app._append_chat = lambda speaker, message: messages.append((speaker, message))

        ConfigApp.send_chat_message(app)

        self.assertEqual(app.chat_input.value, "")
        self.assertEqual(messages[0], ("你", "帮助"))
        self.assertEqual(messages[1][0], "Agent")
        self.assertIn("可用动作", messages[1][1])

    def test_model_decision_does_not_route_to_local_actions(self) -> None:
        app = object.__new__(ConfigApp)
        app.pending_proposals = ()
        messages: list[str] = []
        app._append_chat = lambda speaker, message: messages.append(message)

        ConfigApp._execute_gui_decision(app, LLMDecision(action="run", message="不应运行"))

        self.assertEqual(messages, ["模型响应不符合受限策略，未执行任何操作。"])

    def test_contract_request_without_saved_project_does_not_save_or_submit(self) -> None:
        app = object.__new__(ConfigApp)
        app.current_config_path = None
        messages: list[str] = []
        app._append_chat = lambda speaker, message: messages.append(message)
        app.save_config_silent = lambda: (_ for _ in ()).throw(AssertionError("must not save"))

        with patch("rnaseq_agent.gui.inspect_llm_submission") as inspect, patch(
            "rnaseq_agent.gui.submit_llm_contract"
        ) as submit:
            ConfigApp._execute_gui_decision(
                app,
                LLMDecision(action="contract_submission_request", message="提交当前合同。"),
            )

        inspect.assert_not_called()
        submit.assert_not_called()
        self.assertTrue(any("已保存" in message for message in messages))

        app = object.__new__(ConfigApp)
        app.pending_proposals = (ConfigPatch("/sequencing/layout", "single"),)
        app._append_chat = lambda *_: None
        app.build_config = lambda: (_ for _ in ()).throw(AssertionError("must not build config"))

        with patch("rnaseq_agent.gui.messagebox.askyesno", return_value=False):
            ConfigApp.apply_pending_proposals(app)

        self.assertEqual(app.pending_proposals[0].value, "single")

    def test_discard_clears_without_configuration_or_network_work(self) -> None:
        app = object.__new__(ConfigApp)
        app.pending_proposals = (ConfigPatch("/pipeline/arriba/enabled", False),)
        messages: list[str] = []
        app._append_chat = lambda speaker, message: messages.append(message)

        ConfigApp.discard_pending_proposals(app)

        self.assertEqual(app.pending_proposals, ())
        self.assertEqual(messages, ["已丢弃待审阅的配置建议。"])
    def test_onboarding_skip_advances_without_writing_or_network_work(self) -> None:
        app = object.__new__(ConfigApp)
        app.onboarding_active = True
        app.onboarding_field_index = 2
        app.onboarding_turn = 4
        app.pending_onboarding_proposals = (OnboardingPatch("server.host", "hpc.example.edu"),)
        messages: list[str] = []
        app._append_chat = lambda _speaker, message: messages.append(message)
        app._ask_onboarding_question = lambda: messages.append("asked-next")
        app.build_config = lambda: (_ for _ in ()).throw(AssertionError("must not build config"))
        app._apply_loaded_config = lambda _config: (_ for _ in ()).throw(AssertionError("must not apply config"))
        app.save_config_silent = lambda: (_ for _ in ()).throw(AssertionError("must not save config"))

        handled = ConfigApp._handle_local_onboarding_skip(app, "跳过这一项我自己填")

        self.assertTrue(handled)
        self.assertEqual(app.onboarding_field_index, 3)
        self.assertEqual(app.onboarding_turn, 5)
        self.assertEqual(app.pending_onboarding_proposals, ())
        self.assertIn("当前字段未修改", messages[0])
        self.assertEqual(messages[1], "asked-next")

    def test_stale_onboarding_response_does_not_modify_form_or_network(self) -> None:
        app = object.__new__(ConfigApp)
        app.onboarding_active = True
        app.onboarding_turn = 4
        app.pending_onboarding_proposals = ()
        messages: list[str] = []
        app._append_chat = lambda _speaker, message: messages.append(message)
        app.build_config = lambda: (_ for _ in ()).throw(AssertionError("must not build config"))

        ConfigApp._receive_onboarding_decision(
            app,
            OnboardingDecision("已提取", (OnboardingPatch("server.host", "hpc.example.edu"),)),
            3,
        )

        self.assertEqual(app.pending_onboarding_proposals, ())
        self.assertEqual(messages, [])

    def test_downstream_rejection_does_not_build_or_connect(self) -> None:
        app = object.__new__(ConfigApp)
        app._downstream_review_text = lambda: "本地审阅内容"
        app.build_config = lambda: (_ for _ in ()).throw(AssertionError("must not build config"))

        with patch("rnaseq_agent.gui.messagebox.askyesno", return_value=False), patch(
            "rnaseq_agent.gui.downstream_errors"
        ) as errors:
            ConfigApp.apply_downstream_configuration(app)

        errors.assert_not_called()

    def test_downstream_submission_rejection_prevents_remote_effects(self) -> None:
        class Variable:
            def get(self) -> bool:
                return True

        app = object.__new__(ConfigApp)
        app.running = False
        app.downstream_enabled = Variable()
        app._downstream_review_text = lambda: "下游本地审阅摘要"
        app._prepare_server_credential = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not prepare credentials"))
        app.save_config_silent = lambda: (_ for _ in ()).throw(AssertionError("must not save config"))

        with patch("rnaseq_agent.gui.messagebox.askyesno", return_value=False) as confirm:
            ConfigApp.start_run(app, wait=False)

        self.assertIn("下游本地审阅摘要", confirm.call_args.args[1])
        self.assertIn("不会在登录节点直接运行", confirm.call_args.args[1])

    def test_onboarding_apply_requires_explicit_confirmation(self) -> None:
        app = object.__new__(ConfigApp)
        app.pending_onboarding_proposals = (OnboardingPatch("server.host", "hpc.example.edu"),)
        app._append_chat = lambda *_: None
        app.build_config = lambda: (_ for _ in ()).throw(AssertionError("must not build config"))

        with patch("rnaseq_agent.gui.messagebox.askyesno", return_value=False):
            ConfigApp.apply_pending_onboarding_proposals(app)

        self.assertEqual(app.pending_onboarding_proposals[0].value, "hpc.example.edu")

    def test_species_answer_is_handled_locally_before_model_routing(self) -> None:
        app = object.__new__(ConfigApp)
        app.awaiting_species_answer = True
        app.pending_reference_candidates = ()
        app._render_reference_candidates = lambda: None
        messages: list[str] = []
        app._append_chat = lambda _role, message: messages.append(message)

        handled = ConfigApp._handle_local_species_answer(app, "小鼠")

        self.assertTrue(handled)
        self.assertEqual(
            app.pending_reference_candidates[0].catalog_id,
            "GENCODE_M35_GRCm39_PRIMARY",
        )
        self.assertFalse(app.awaiting_species_answer)
        self.assertTrue(any("本地" in message for message in messages))

    def test_unsupported_assembly_does_not_prompt_species_or_model(self) -> None:
        app = object.__new__(ConfigApp)
        app.pending_reference_candidates = ()
        app.awaiting_species_answer = True
        app.reference_candidate_text = None
        messages: list[str] = []
        app._append_chat = lambda _role, message: messages.append(message)

        ConfigApp._stage_reference_candidates(
            app,
            (),
            unsupported_assembly=True,
        )

        self.assertFalse(app.awaiting_species_answer)
        self.assertTrue(any("GRCm39" in message for message in messages))

    def test_catalog_application_rejection_leaves_form_unchanged(self) -> None:
        class Variable:
            def __init__(self, value: str) -> None:
                self.value = value

            def set(self, value: str) -> None:
                self.value = value

        app = object.__new__(ConfigApp)
        app.pending_reference_candidates = candidates_for_species("mouse")
        app.awaiting_species_answer = False
        app.reference_candidate_text = None
        app.ref_vars = {"name": Variable("manual_reference")}
        app._append_chat = lambda *_: None
        app.build_config = lambda: (_ for _ in ()).throw(AssertionError("must not build config"))

        with patch("rnaseq_agent.gui.messagebox.askyesno", return_value=False):
            ConfigApp.apply_pending_reference_catalog(app)

        self.assertEqual(app.ref_vars["name"].value, "manual_reference")
        self.assertEqual(len(app.pending_reference_candidates), 1)

    def test_catalog_application_updates_only_reference_form(self) -> None:
        class Variable:
            def __init__(self, value: str = "") -> None:
                self.value = value

            def set(self, value: str) -> None:
                self.value = value

        app = object.__new__(ConfigApp)
        app.pending_reference_candidates = candidates_for_species("mouse")
        app.awaiting_species_answer = False
        app.reference_candidate_text = None
        app.ref_vars = {
            "name": Variable(),
            "species": Variable(),
            "star_index_dir": Variable(),
        }
        app._append_chat = lambda *_: None
        app.build_config = lambda: (_ for _ in ()).throw(AssertionError("must not build config"))

        with patch("rnaseq_agent.gui.messagebox.askyesno", return_value=True):
            ConfigApp.apply_pending_reference_catalog(app)

        self.assertEqual(app.ref_vars["name"].value, "GENCODE_M35_GRCm39_PRIMARY")
        self.assertEqual(app.ref_vars["species"].value, "mouse")
        self.assertEqual(app.ref_vars["star_index_dir"].value, "")
        self.assertEqual(app.pending_reference_candidates, ())


if __name__ == "__main__":
    unittest.main()
