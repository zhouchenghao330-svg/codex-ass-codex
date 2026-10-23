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
fn centered_reading_preserves_position_and_original_colors() {
    use ratatui::{Terminal, backend::TestBackend};
    let text = (0..40)
        .map(|n| format!("READ_{n:02} {}", "米白色正文与正常阅读位置。".repeat(18)))
        .collect::<Vec<_>>()
        .join("\n\n");
    let mut app = app(vec![entry("history", "A", &text)]);
    let mut terminal = Terminal::new(TestBackend::new(160, 40)).unwrap();
    terminal.draw(|f| app.draw(f)).unwrap();
    let top = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text.starts_with("READ_15"))
        .unwrap()
        + 1;
    let prefix = app.timeline.rows[top]
        .text
        .chars()
        .take(16)
        .collect::<String>();
    app.scroll = top;
    app.follow = false;
    app.pending_bottom = false;
    app.on_service(json!({"type":"action","action":"reading_centered","enabled":true}));
    terminal.draw(|f| app.draw(f)).unwrap();
    assert_eq!(app.body.width, 95);
    assert_eq!(app.body.x, (160 - 95) / 2);
    assert_eq!(app.timeline.width, 92);
    assert_eq!(app.input_area.x, 0);
    assert_eq!(app.input_area.width, 160);
    assert!(!app.follow);
    let visible = app
        .timeline
        .rows
        .iter()
        .skip(app.scroll)
        .take(4)
        .map(|r| r.text.as_str())
        .collect::<String>();
    assert!(
        visible.contains(&prefix),
        "reading position moved: {visible}"
    );
    let cell = &terminal.backend().buffer()[(app.body.x + 2, app.body.y)];
    assert_eq!(cell.fg, FG);
    assert_eq!(cell.bg, BG);
    let point = app.point(app.body.x + 2, app.body.y).unwrap();
    assert_eq!(point.row, app.scroll);
    assert_eq!(point.byte, 0);
    app.sidebar = true;
    terminal.draw(|f| app.draw(f)).unwrap();
    assert_eq!(app.body.x, 24 + (160 - 24 - 95) / 2);
    assert_eq!(app.body.width, 95);
    assert_eq!(app.input_area.x, 24);
    assert_eq!(app.input_area.width, 136);
    app.on_service(json!({"type":"action","action":"reading_centered","enabled":false}));
    terminal.draw(|f| app.draw(f)).unwrap();
    assert_eq!(app.body.x, 24);
    assert_eq!(app.body.width, 136);
    assert!(!app.follow);
}

#[test]
fn centered_reading_restores_and_shrinks_to_available_width() {
    use ratatui::{Terminal, backend::TestBackend};
    let mut app = app(vec![entry("reply", "A", "阅读颜色测试")]);
    app.on_service(
        json!({"type":"init","cwd":"/example","entries":app.entries,"reading_centered":true}),
    );
    for (width, sidebar) in [
        (160, false),
        (120, true),
        (80, false),
        (80, true),
        (50, false),
    ] {
        app.sidebar = sidebar;
        let mut terminal = Terminal::new(TestBackend::new(width, 32)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        let available = width - if sidebar && width >= 65 { 24 } else { 0 };
        assert_eq!(app.body.width, available.min(95));
        assert_eq!(
            app.timeline.width,
            available.min(95).saturating_sub(3) as usize
        );
        let row = app
            .timeline
            .rows
            .iter()
            .position(|r| r.text == "阅读颜色测试")
            .unwrap();
        let cell =
            &terminal.backend().buffer()[(app.body.x + 2, app.body.y + (row - app.scroll) as u16)];
        assert_eq!(cell.fg, FG);
        assert_eq!(cell.bg, BG);
    }
}
#[test]
fn markdown_tables_fit_chat_columns_after_sidebar_and_window_resize() {
    use ratatui::{Terminal, backend::TestBackend, style::Modifier};
    use unicode_width::UnicodeWidthStr;

    let source = include_str!("../../tests/fixtures/markdown_table.md");
    let legacy = entry("legacy", "A", source);
    let mut reply = entry("reply", "A", source);
    reply["blocks"] = json!([{"id":"text","kind":"text","text":source}]);
    let mut peer = entry("peer", "A", source);
    peer["target"] = json!("B");
    peer["communication"] = json!({"content_available":true,"target_label":"B"});
    let mut app = app(vec![legacy, reply, peer]);

    for (columns, sidebar, centered) in [
        (160, false, false),
        (160, true, false),
        (80, true, false),
        (80, false, false),
        (160, false, true),
        (160, true, true),
        (80, true, true),
        (160, false, false),
    ] {
        app.sidebar = sidebar;
        app.reading_centered = centered;
        let mut terminal = Terminal::new(TestBackend::new(columns, 50)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let available = columns - if sidebar { 24 } else { 0 };
        let width = (if centered {
            available.min(95)
        } else {
            available
        } - 3) as usize;
        assert_eq!(app.timeline.width, width);
        for message in 0..3 {
            let rows = app
                .timeline
                .rows
                .iter()
                .filter(|r| r.message == message && r.action.is_empty() && !r.text.is_empty())
                .collect::<Vec<_>>();
            assert!(rows.len() > 6);
            assert!(
                rows[0].text.starts_with('┌') && rows[0].text.ends_with('┐'),
                "broken top border at {width} columns: {}",
                rows[0].text
            );
            assert!(rows.last().unwrap().text.ends_with('┘'));
            let boundaries = |text: &str| {
                text.char_indices()
                    .filter(|(_, c)| "│┌┬┐├┼┤└┴┘".contains(*c))
                    .map(|(byte, _)| text[..byte].width())
                    .collect::<Vec<_>>()
            };
            let expected = boundaries(&rows[0].text);
            assert_eq!(expected.len(), 4);
            for row in &rows {
                assert!(row.line.width() <= width);
                assert_eq!(
                    boundaries(&row.text),
                    expected,
                    "misaligned row: {}",
                    row.text
                );
            }
            let column_text = |column: usize| {
                rows.iter()
                    .filter(|r| r.text.starts_with('│'))
                    .map(|r| r.text.split('│').nth(column + 1).unwrap())
                    .collect::<String>()
                    .chars()
                    .filter(|c| !c.is_whitespace())
                    .collect::<String>()
            };
            assert!(column_text(0).contains("接口方案A"));
            assert!(column_text(1).contains("中文和English混排"));
            assert!(column_text(2).contains("https://example.com/reference/very-long-continuous-path/without-any-spaces/terminal-markdown-table-width#END_OF_LONG_URL"));
            assert!(rows.iter().flat_map(|r| &r.line.spans).any(
                |s| s.content.contains("接口") && s.style.add_modifier.contains(Modifier::BOLD)
            ));
            assert!(
                rows.iter()
                    .flat_map(|r| &r.line.spans)
                    .any(|s| s.content.contains("https")
                        && s.style.add_modifier.contains(Modifier::UNDERLINED))
            );
            assert_eq!(app.entries[message]["body"], source);
            assert!(
                rows.iter().all(|r| r.table.is_some()),
                "table mapping missing at width {width}"
            );
            let first = app
                .timeline
                .rows
                .iter()
                .position(|r| std::ptr::eq(r, *rows.first().unwrap()))
                .unwrap();
            let last = first + rows.len() - 1;
            let copied = table_copy::selected_text(
                Selection {
                    anchor: Point {
                        row: first,
                        byte: 0,
                    },
                    head: Point {
                        row: last,
                        byte: rows.last().unwrap().text.len(),
                    },
                },
                rows.iter().enumerate().map(|(n, r)| (first + n, *r)),
            );
            assert!(!copied.contains('│') && !copied.contains('┌'), "{copied}");
            assert!(copied.contains("https://example.com/reference/very-long-continuous-path/without-any-spaces/terminal-markdown-table-width#END_OF_LONG_URL"), "{copied}");
        }
    }
}

#[test]
fn table_clipboard_and_forwarding_preserve_only_selected_characters() {
    let url = format!(
        "https://example.com/{}#",
        "abcdefghijklmnopqrstuvwxyz".repeat(4)
    );
    let source = format!(
        "before\n\n| 名称 | 内容 |\n| :--- | ---: |\n| 中文 | [参考]({url}) |\n| next | 留在原处 |\n\nafter"
    );
    let mut reply = entry("reply", "A", &source);
    reply["blocks"] = json!([{"id":"text","kind":"text","text":source}]);
    let mut app = app(vec![reply]);
    app.timeline.width = 32;
    app.timeline.invalidate();
    app.rebuild = true;
    app.rebuild();
    let first = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text.contains("https"))
        .unwrap();
    let last = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text.contains('#'))
        .unwrap();
    assert!(last > first + 3);
    let a = app.timeline.rows[first].text.find("https").unwrap();
    let b = app.timeline.rows[last].text.find('#').unwrap() + 1;
    select_rows(&mut app, first, a, last, b);
    assert_eq!(app.selected_text(), url);
    let quote = app.quote().unwrap();
    assert_eq!(quote["messages"][0]["text"], url);
    assert_eq!(quote["messages"][0]["blocks"][0]["text"], url);
    // Reverse dragging and endpoints inside the URL must not expand the range.
    app.selection = Some(Selection {
        anchor: Point {
            row: last,
            byte: b - 1,
        },
        head: Point {
            row: first,
            byte: a + 4,
        },
    });
    assert_eq!(app.selected_text(), &url[4..url.len() - 1]);
    let row = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text.contains("中文"))
        .unwrap();
    let a = app.timeline.rows[row].text.find("中文").unwrap();
    select_rows(&mut app, row, a, row, a + "中".len());
    assert_eq!(app.selected_text(), "中");
    select_rows(&mut app, first, 0, first, '│'.len_utf8());
    assert_eq!(app.selected_text(), "");
}

#[test]
fn table_copy_preserves_word_spaces_empty_cells_alignment_and_inline_syntax() {
    let source = "| L | C | R |\n| :-- | :-: | --: |\n| alpha  beta gamma delta | **中文** `code` &amp; x\\|y │ | [说明][ref] |\n| | 留下 | |\n\n[ref]: https://example.com/some/long/path?q=1&x=2";
    let expected = "L\tC\tR\nalpha  beta gamma delta\t中文 code & x|y │\t说明 (https://example.com/some/long/path?q=1&x=2)\n\t留下\t";
    for prefix in ["", "> ", "  "] {
        let source = source
            .lines()
            .map(|line| format!("{prefix}{line}"))
            .collect::<Vec<_>>()
            .join("\n");
        for width in [8, 12, 24, 47, 80, 157] {
            let mut app = app(vec![entry("reply", "A", &source)]);
            app.timeline.width = width;
            app.timeline.invalidate();
            app.rebuild = true;
            app.rebuild();
            let first = app
                .timeline
                .rows
                .iter()
                .position(|r| r.text.contains('┌'))
                .unwrap();
            let last = app
                .timeline
                .rows
                .iter()
                .position(|r| r.text.contains('┘'))
                .unwrap();
            assert!(
                app.timeline.rows[first..=last]
                    .iter()
                    .all(|r| r.table.is_some()),
                "mapping {prefix:?} at {width}"
            );
            let end = app.timeline.rows[last].text.len();
            select_rows(&mut app, first, 0, last, end);
            assert_eq!(app.selected_text(), expected, "{prefix:?} at {width}");
            assert_eq!(app.entries[0]["body"], source);
        }
    }
}

#[test]
fn mixed_table_selection_keeps_prose_code_and_separate_tables() {
    let source = "before\n\n```text\n┌───┐\n│ X │\n├───┤\n│ Y │\n└───┘\n```\n\n| X |\n| - |\n| Y |\n\nbetween\n\n| X |\n| - |\n| Z |\n\nafter";
    let mut app = app(vec![entry("reply", "A", source)]);
    let first = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text == "before")
        .unwrap();
    let last = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text == "after")
        .unwrap();
    let end = app.timeline.rows[last].text.len();
    select_rows(&mut app, first, 0, last, end);
    assert_eq!(
        app.selected_text(),
        "before\n\n```text\n┌───┐\n│ X │\n├───┤\n│ Y │\n└───┘\n```\n\nX\nY\n\nbetween\n\nX\nZ\n\nafter"
    );
}

#[test]
fn table_copy_handles_graphemes_images_math_and_footnotes_in_a_list() {
    let source = "- | 内容 | 引用 |\n  | - | - |\n  | 👩‍💻é界 **粗体** | ![图](img.png) $x$ 参考[^n] |\n\n[^n]: 注释正文";
    for width in [10, 24, 80] {
        let mut app = app(vec![entry("reply", "A", source)]);
        app.timeline.width = width;
        app.timeline.invalidate();
        app.rebuild = true;
        app.rebuild();
        let first = app
            .timeline
            .rows
            .iter()
            .position(|r| r.text.contains('┌'))
            .unwrap();
        let last = app
            .timeline
            .rows
            .iter()
            .position(|r| r.text.contains('┘'))
            .unwrap();
        assert!(
            app.timeline.rows[first..=last]
                .iter()
                .all(|r| r.table.is_some()),
            "mapping {width}"
        );
        let end = app.timeline.rows[last].text.len();
        select_rows(&mut app, first, 0, last, end);
        assert_eq!(
            app.selected_text(),
            "内容\t引用\n👩‍💻é界 粗体\t[img] 图 $x$ 参考[n]",
            "{width}"
        );
    }
}
#[test]
fn copy_shortcuts_accept_macos_command_and_control_without_plain_c() {
    for modifiers in [
        KeyModifiers::SUPER,
        KeyModifiers::META,
        KeyModifiers::CONTROL,
        KeyModifiers::CONTROL | KeyModifiers::SHIFT,
    ] {
        assert!(is_copy_key(KeyEvent::new(KeyCode::Char('c'), modifiers)));
        assert!(is_copy_key(KeyEvent::new(KeyCode::Char('C'), modifiers)));
    }
    assert!(!is_copy_key(KeyEvent::new(
        KeyCode::Char('c'),
        KeyModifiers::NONE
    )));
    assert!(!is_copy_key(KeyEvent::new(
        KeyCode::Char('c'),
        KeyModifiers::ALT
    )));
    assert!(!is_copy_key(KeyEvent::new(
        KeyCode::Char('v'),
        KeyModifiers::SUPER
    )));
}
#[test]
fn command_copy_with_no_selection_keeps_app_and_draft() {
    let mut app = app(vec![]);
    app.draft.input.insert("尚未发送");
    app.copy_selection(false);
    assert!(!app.quit);
    assert_eq!(app.draft.input.text, "尚未发送");
    assert!(app.notice.contains("先拖选"));
    app.copy_selection(true);
    assert!(app.quit);
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
fn selecting_prose_keeps_live_working_phase_and_elapsed_time() {
    use ratatui::{Terminal, backend::TestBackend};
    use unicode_width::UnicodeWidthStr;

    let mut answer = entry("answer", "A", "需要选中的正文");
    answer["state"] = json!("working");
    answer["started_epoch"] = json!(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()
            - 28.
    );
    let mut app = app(vec![answer.clone()]);
    app.members[0]["busy"] = json!(true);
    app.members[0]["phase"] = json!("正在思考：分析分支");
    let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
    let mut working_line = |app: &mut App| {
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let buffer = terminal.backend().buffer();
        (0..24)
            .find_map(|y| {
                let mut text = String::new();
                let mut x = 0;
                while x < 100 {
                    let symbol = buffer[(x, y)].symbol();
                    text.push_str(symbol);
                    x += symbol.width().max(1) as u16;
                }
                text.contains("Working").then_some(text)
            })
            .unwrap()
    };
    assert!(working_line(&mut app).contains("Working · 正在思考：分析分支 ·"));
    let row = app
        .timeline
        .rows
        .iter()
        .position(|r| r.text == "需要选中的正文")
        .unwrap();
    select_rows(&mut app, row, 0, row, "需要选中的正文".len());
    let line = working_line(&mut app);
    assert!(line.contains("Working · 正在思考：分析分支 ·"), "{line}");
    assert!(line.trim_end().ends_with('s'));
    answer["body"] = json!("需要选中的正文\n\n随后生成的新输出");
    app.on_service(json!({"type":"patch","entries":[answer]}));
    let mut members = app.members.clone();
    members[0]["phase"] = json!("正在执行命令…");
    app.on_service(
        json!({"type":"members","agents":members,"selected":"A","show_all":false,"connected":true}),
    );
    assert!(working_line(&mut app).contains("Working · 正在执行命令… ·"));
    assert_eq!(app.selected_text(), "需要选中的正文");
    assert!(
        !app.timeline
            .rows
            .iter()
            .any(|r| r.text.contains("随后生成的新输出"))
    );
    app.clear_selection();
    working_line(&mut app);
    assert!(
        app.timeline
            .rows
            .iter()
            .any(|r| r.text.contains("随后生成的新输出"))
    );
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
