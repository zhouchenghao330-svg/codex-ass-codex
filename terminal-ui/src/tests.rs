use super::*;
fn app(entries: Vec<Value>) -> App {
    let mut app = App::new(80);
    app.members = vec![
        json!({"name":"A","label":"A","cwd":"/example/project-b","color":"#a9b6ff"}),
        json!({"name":"B","label":"B","cwd":"/example","color":"#78d8bc"}),
    ];
    app.selected = "A".into();
    app.show_all = false;
    app.on_service(json!({"type":"init","cwd":"/example","entries":entries}));
    app.rebuild();
    app
}
fn entry(id: &str, sender: &str, body: &str) -> Value {
    json!({"id":id,"sender":sender,"body":body,"target":if sender=="you"{"A"}else{""},"state":"done"})
}
fn select_rows(app: &mut App, first: usize, a: usize, last: usize, b: usize) {
    app.selection = Some(Selection {
        anchor: Point {
            row: first,
            byte: a,
        },
        head: Point { row: last, byte: b },
    });
}
#[test]
fn member_name_search_is_not_confused_by_another_members_directory() {
    let app = app(vec![]);
    let items = app.member_items();
    let found = filtered_items(&items, "B");
    assert_eq!(found.len(), 1);
    assert_eq!(found[0].value, "B");
}
#[test]
fn native_children_keep_identity_and_internal_relays_are_hidden() {
    let mut hidden = entry("internal", "you", "PRIVATE_RELAY_ENVELOPE");
    hidden["hidden"] = json!(true);
    let mut app = app(vec![
        entry("source", "A", "source discussion"),
        hidden,
        entry("child", "B", "original child opinion"),
    ]);
    app.members[1]["parent_name"] = json!("A");
    app.show_all = true;
    app.timeline.invalidate();
    app.rebuild();
    assert!(
        !app.timeline
            .rows
            .iter()
            .any(|r| r.text.contains("PRIVATE_RELAY_ENVELOPE"))
    );
    let last = app.timeline.rows.len() - 1;
    let end = app.timeline.rows[last].text.len();
    select_rows(&mut app, 0, 0, last, end);
    let quote = app.quote().unwrap();
    assert!(!quote.to_string().contains("PRIVATE_RELAY_ENVELOPE"));
    assert!(app.member_items()[1].detail.contains("子代理 ↳ @A"));
}
#[test]
fn stale_status_updates_do_not_undo_a_new_view_choice() {
    let mut app = app(vec![]);
    app.selected = "B".into();
    app.view_revision = 4;
    app.on_service(json!({"type":"members","agents":app.members,"selected":"A","show_all":true,"connected":true,"view_revision":3}));
    assert_eq!(app.selected, "B");
    assert!(!app.show_all);
    app.on_service(json!({"type":"members","agents":app.members,"selected":"B","show_all":false,"connected":true,"view_revision":4}));
    assert_eq!(app.selected, "B");
}
#[test]
fn communication_text_direction_and_missing_payload_survive_forwarding() {
    let mut message = entry("message", "A", "**Review this**");
    message["target"] = json!("B");
    message["communication"] =
        json!({"action":"发送消息","content_available":true,"target_label":"B"});
    message["blocks"] = json!([{"id":"message","kind":"text","text":"**Review this**"}]);
    let mut app = app(vec![message]);
    app.selected = "B".into();
    app.timeline.invalidate();
    app.rebuild = true;
    app.rebuild();
    assert!(app.timeline.rows.iter().any(|r| r.text.contains("@A → @B")));
    let last = app.timeline.rows.len() - 1;
    select_rows(&mut app, 0, 0, last, 0);
    let quote = app.quote().unwrap();
    assert_eq!(quote["source"], "B");
    assert_eq!(quote["messages"][0]["member"], "B");
    assert_eq!(quote["messages"][0]["speaker"], "A");
    assert_eq!(quote["messages"][0]["recipient"], "B");
    app.clear_selection();
    app.entries[0]["body"] = json!("");
    app.entries[0]["blocks"] = json!([]);
    app.entries[0]["communication"]["content_available"] = json!(false);
    app.timeline.invalidate();
    app.rebuild = true;
    app.rebuild();
    assert!(
        app.timeline
            .rows
            .iter()
            .any(|r| r.text.contains("Codex 未提供"))
    );
    let last = app.timeline.rows.len() - 1;
    select_rows(&mut app, 0, 0, last, 0);
    let quote = app.quote().unwrap();
    assert_eq!(quote["messages"][0]["text"], "");
    assert_eq!(
        quote["messages"][0]["communication"]["content_available"],
        false
    );
}
#[test]
fn acknowledgement_retains_new_input_and_rejection_restores_atomic_payload() {
    let mut app = app(vec![]);
    let mut submitted = Draft::default();
    submitted.input.paste(&"中".repeat(6000), &[]);
    let prompt = submitted.input.prompt();
    app.submit_pending = Some(Submission {
        id: 1,
        prompt: prompt.clone(),
        view: "A".into(),
        draft: submitted.clone(),
    });
    app.draft.input.insert("下一条");
    app.on_service(json!({"type":"submitted","id":1,"accepted":true}));
    assert_eq!(app.draft.input.text, "下一条");
    assert_eq!(app.input_history.last(), Some(&prompt));
    app.submit_pending = Some(Submission {
        id: 2,
        prompt: prompt.clone(),
        view: "A".into(),
        draft: submitted,
    });
    app.on_service(json!({"type":"submitted","id":2,"accepted":false}));
    assert_eq!(app.draft.input.attachments.len(), 1);
    assert!(app.draft.input.expanded_text().ends_with("下一条"));
    assert_eq!(app.draft.input.expanded_text().chars().count(), 6004);
}
#[test]
fn partial_forward_does_not_include_prose_outside_selection() {
    let mut e = entry("a", "A", "before\n\nchosen\n\nafter");
    e["blocks"] = json!([{"id":"body","kind":"text","text":"before\n\nchosen\n\nafter"}]);
    let mut app = app(vec![e]);
    let at = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text == "chosen")
        .unwrap();
    select_rows(&mut app, at, 0, at, 6);
    let q = app.quote().unwrap();
    assert_eq!(q["messages"][0]["blocks"][0]["text"], "chosen");
    assert!(!q.to_string().contains("before"));
    assert!(!q.to_string().contains("after"));
}
#[test]
fn selection_across_a_collapsed_tool_keeps_its_full_output() {
    let mut e = entry("a", "A", "intro\nconclusion");
    e["blocks"] = json!([{"id":"i","kind":"text","text":"intro"},{"id":"tool","kind":"command","title":"pwd","text":"HIDDEN_FULL_OUTPUT","status":"completed"},{"id":"c","kind":"text","text":"conclusion"}]);
    let mut app = app(vec![entry("q", "you", "problem"), e]);
    let first = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text == "problem")
        .unwrap();
    let last = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text == "conclusion")
        .unwrap();
    select_rows(&mut app, first, 0, last, 10);
    let q = app.quote().unwrap();
    assert!(q.to_string().contains("HIDDEN_FULL_OUTPUT"));
    assert!(!app.selected_text().contains("HIDDEN_FULL_OUTPUT"));
    assert_eq!(q["messages"][0]["text"], "problem");
    assert!(!q.to_string().contains("Worked for"));
}
#[test]
fn wrapped_prompt_selection_preserves_original_newlines_and_paste_payload() {
    let mut input = Input::default();
    input.insert("不要转发这个前缀\n");
    input.paste(&"原文".repeat(1000), &[]);
    input.insert("\n也不要转发后缀");
    let mut e = entry("q", "you", &input.expanded_text());
    e["display_body"] = json!(input.text);
    e["attachments"] = input.prompt()["attachments"].clone();
    let mut app = app(vec![e]);
    app.timeline.width = 12;
    app.timeline.invalidate();
    app.rebuild = true;
    app.rebuild();
    let first = app
        .timeline
        .rows
        .iter()
        .position(|r| r.source_start == Some("不要转发这个前缀\n".len()))
        .unwrap();
    let last = app
        .timeline
        .rows
        .iter()
        .rposition(|r| r.source_start.is_some_and(|p| p < input.attachments[0].end))
        .unwrap();
    let end = app.timeline.rows[last].text.len();
    select_rows(&mut app, first, 0, last, end);
    let q = app.quote().unwrap();
    assert_eq!(
        q["messages"][0]["text"].as_str().unwrap(),
        "原文".repeat(1000)
    );
    assert_eq!(app.selected_text(), "原文".repeat(1000));
    assert!(!q.to_string().contains("前缀"));
    assert!(!q.to_string().contains("后缀"));
}
#[test]
fn stream_updates_do_not_move_drag_anchor_or_reparse_selected_history() {
    let mut app = app(vec![entry("a", "A", "old text")]);
    let at = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text == "old text")
        .unwrap();
    select_rows(&mut app, at, 0, at, 3);
    app.on_service(
        json!({"type":"patch","entries":[entry("a","A","old text\n\nnew streaming text")]}),
    );
    app.rebuild();
    assert_eq!(app.timeline.rows[at].text, "old text");
    assert!(
        !app.timeline
            .rows
            .iter()
            .any(|r| r.text == "new streaming text")
    );
    app.clear_selection();
    app.rebuild();
    assert!(
        app.timeline
            .rows
            .iter()
            .any(|r| r.text == "new streaming text")
    );
}
#[test]
fn ten_thousand_history_messages_stay_cached_while_tail_streams() {
    let entries = (0..10000)
        .map(|i| entry(&format!("m{i}"), "A", &format!("稳定历史 {i}")))
        .collect();
    let mut app = app(entries);
    let pointer = app.timeline.rows[1].text.as_ptr();
    for i in 0..20 {
        app.on_service(
            json!({"type":"patch","entries":[entry("m9999","A",&format!("新的流式文字 {i}"))]}),
        );
        app.rebuild();
        assert_eq!(app.timeline.rows[1].text.as_ptr(), pointer);
    }
}
#[test]
fn grouped_user_card_updates_all_recipients_states() {
    let mut a = entry("qA", "you", "shared");
    a["batch_id"] = json!("batch");
    a["state"] = json!("queued");
    let mut b = a.clone();
    b["id"] = json!("qB");
    b["target"] = json!("B");
    let mut app = app(vec![a, b.clone()]);
    app.show_all = true;
    app.timeline.invalidate();
    app.rebuild = true;
    app.rebuild();
    b["state"] = json!("steered");
    app.on_service(json!({"type":"patch","entries":[b]}));
    app.rebuild();
    let heading = &app.timeline.rows[0].text;
    assert!(heading.contains("@B 已追加"));
    assert!(heading.contains("@A 待追加"));
}
