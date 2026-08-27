from __future__ import annotations

import json

from lemonbot.connectors.pywechat_probe import UIANodeFacts, analyze_pywechat_nodes


def _exposed_nodes() -> list[UIANodeFacts]:
    return [
        UIANodeFacts(control_type="Window", class_name="mmui::MainWindow", framework_id="Qt"),
        UIANodeFacts(
            control_type="List", class_name="private-contact-list", framework_id="Qt", name="会话"
        ),
        UIANodeFacts(control_type="List", class_name="private-message-list", name="消息"),
        UIANodeFacts(
            control_type="Edit",
            class_name="mmui::XValidatorTextEdit",
            framework_id="Qt",
            name="搜索",
        ),
        UIANodeFacts(
            control_type="Edit",
            class_name="mmui::ChatInputField",
            automation_id="chat_input_field",
            name="secret draft text",
        ),
        UIANodeFacts(control_type="Button", class_name="mmui::XButton", name="发送"),
        UIANodeFacts(
            control_type="Text",
            automation_id=(
                "content_view.top_content_view.title_h_view.left_v_view."
                "left_content_v_view.left_ui_.big_title_line_h_view.current_chat_name_label"
            ),
            name="private contact name",
        ),
        UIANodeFacts(
            control_type="ListItem",
            class_name="mmui::ChatSessionCell",
            automation_id="session_item_private contact name",
            name="private message preview",
        ),
    ]


def test_pywechat_probe_recognises_exposed_action_surface_without_leaking_text() -> None:
    report = analyze_pywechat_nodes(
        _exposed_nodes(),
        process_count=6,
        candidate_window_count=1,
        selected_window_handle=123,
        max_depth_seen=8,
        truncated=False,
    )

    assert report.accessibility_exposed
    assert report.observe_surface_ready
    assert report.draft_surface_ready
    assert report.reply_surface_visible
    assert report.status == "pywechat_action_surface_visible"
    encoded = json.dumps(report.safe_dict(), ensure_ascii=False)
    assert "private contact name" not in encoded
    assert "private message preview" not in encoded
    assert "secret draft text" not in encoded


def test_pywechat_probe_reports_current_custom_render_shell_as_not_exposed() -> None:
    nodes = [
        UIANodeFacts(control_type="Window", class_name="Qt51514QWindowIcon"),
        UIANodeFacts(control_type="Pane", class_name="MMUIRenderSubWindowHW"),
        UIANodeFacts(control_type="Pane"),
    ]

    report = analyze_pywechat_nodes(
        nodes,
        process_count=6,
        candidate_window_count=1,
        selected_window_handle=456,
        max_depth_seen=1,
        truncated=False,
    )

    assert not report.accessibility_exposed
    assert not report.observe_surface_ready
    assert report.status == "accessibility_tree_not_exposed"
    assert all(count == 0 for count in report.selector_counts.values())
