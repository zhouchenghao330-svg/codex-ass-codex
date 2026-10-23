# /// script
# requires-python = ">=3.11"
# dependencies = ["pyte==0.8.2", "textual==8.2.8"]
# ///
"""Opt-in real-model Markdown acceptance through the production Rust PTY UI.

Uses a disposable room, gpt-6.1-sol / medium unless overridden, and a private
clipboard capture helper. Does not operate the user's running UI or clipboard.
Run: uv run scripts/check_room_tables_live.py --output artifacts/room-eval-ui
"""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time

from check_terminal_lab import ROOT, Session


URL = "https://example.com/reference/terminal-rendering/very-long-continuous-path/without-any-spaces/markdown-table-width-and-original-copy#END_OF_LONG_URL"


class UI:
    def __init__(self, args):
        self.args = args
        self.output = args.output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="room-eval-ui-")
        self.cwd = Path(self.tmp.name).resolve()
        self.state_path = self.cwd / ".codex-room/state.json"
        self.state_path.parent.mkdir()
        self.state_path.write_text(json.dumps({"version":2,"agents":[{
            "name":name,"label":name,"color":color,"cwd":str(self.cwd),"model":args.model,"effort":args.effort}
            for name,color in (("A","#a9b6ff"),("B","#78d8bc"))],"entries":[],"selected":"A","show_all":False}))
        if args.replay_state:
            state = json.loads(args.replay_state.read_text())
            at = next(i for i,e in enumerate(state["entries"]) if e["sender"]=="A" and URL in e["body"])
            state["entries"] = state["entries"][:at+1]
            state.update(selected="A",show_all=False,drafts={},input_history=[])
            for member in state["agents"]:
                member["cwd"] = str(self.cwd)
            self.state_path.write_text(json.dumps(state))
            self.source = state["entries"][at]["body"]
        self.results = []
        self.session = None
        self.streaming = {}
        self.copied = self.cwd / "clipboard-capture.txt"
        self.copied.write_text("")
        helpers = self.cwd / "helpers"
        helpers.mkdir()
        for name in ("pbcopy","wl-copy","xclip","clip.exe"):
            helper = helpers/name
            helper.write_text('#!/bin/sh\n/bin/cat > "$CODEX_ROOM_COPY_CAPTURE"\n')
            helper.chmod(0o755)
        os.environ.update(CODEX_ROOM_ROOT=str(ROOT),CODEX_ROOM_PYTHON=str(ROOT/".venv/bin/python"),
                          CODEX_ROOM_COPY_CAPTURE=str(self.copied),PATH=str(helpers)+os.pathsep+os.environ["PATH"])
        os.environ.pop("CODEX_ROOM_CODEX_COMMAND",None)
        os.environ.pop("NO_COLOR",None)

    def start(self, columns=160):
        self.session = Session([str(ROOT/"terminal-ui/target/release/codex-room-ui"),"--cwd",str(self.cwd)],columns=columns,rows=100)
        self.session.wait(lambda:"已连接" in self.session.visible,seconds=60)

    def state(self):
        return json.loads(self.state_path.read_text())

    def profile(self):
        assert all((a["model"],a["effort"])==(self.args.model,self.args.effort) for a in self.state()["agents"])

    def submit(self, text):
        self.session.send(b"\x1b[200~"+text.encode()+b"\x1b[201~")
        self.session.read(.1)
        self.session.send(b"\r")
        self.session.wait(lambda:bool(re.search(r"· [1-9]\d* 工作中",self.session.visible)),seconds=90)

    def complete(self, *, probe=False):
        deadline = time.monotonic()+240
        partial_frames = 0
        changed_frames = 0
        previous = ""
        responsive_ms = None
        while time.monotonic()<deadline:
            self.session.read(.05)
            visible = self.session.visible
            if visible != previous:
                changed_frames += 1
                if "│ ┌" in visible and "0 工作中" not in visible:
                    partial_frames += 1
                with (self.output/"frames.jsonl").open("a") as f:
                    f.write(json.dumps({"time":time.monotonic(),"screen":visible},ensure_ascii=False)+"\n")
                previous = visible
            if probe and responsive_ms is None and "│ ┌" in visible and "0 工作中" not in visible:
                begin = time.monotonic()
                self.session.send("输入响应探针".encode())
                self.session.wait(lambda:"输入响应探针" in "\n".join(self.session.visible.splitlines()[-5:]),seconds=5)
                responsive_ms = round((time.monotonic()-begin)*1000,2)
                self.session.send(b"\x15")
                self.session.wait(lambda:"输入响应探针" not in self.session.visible)
            if "0 工作中" in visible:
                self.session.read(.15)
                self.profile()
                self.streaming = {"changed_frames":changed_frames,"partial_table_frames":partial_frames,
                                  "input_probe_ms":responsive_ms}
                return
        raise TimeoutError("real model did not finish in 240 seconds")

    def table_groups(self, columns, sidebar):
        left = (24 if sidebar and columns>=65 else 0)+2
        groups = []
        current = []
        for y in range(self.session.screen.lines):
            row = self.session.screen.buffer[y]
            first = row.get(left)
            if not first:
                continue
            if first.data == "┌":
                current = [(y,row)]
            elif current and first.data in ("│","├","└"):
                current.append((y,row))
                if first.data == "└":
                    groups.append(current)
                    current = []
            elif current:
                current = []
        return left,groups

    def tables_fit(self, columns, sidebar=False):
        borders = "│┌┬┐├┼┤└┴┘"
        def valid():
            # A resized pyte grid can still contain the complete old narrow
            # table. Wait for the new frame's composer border before inspecting.
            corner = self.session.screen.buffer[self.session.screen.lines-2].get(columns-1)
            if not corner or corner.data!="┘":
                return False
            left,groups = self.table_groups(columns,sidebar)
            if not groups:
                return False
            for group in groups:
                expected = None
                for _,row in group:
                    positions = [x for x in range(left,columns) if row.get(x) and row[x].data and row[x].data in borders]
                    if expected is None:
                        expected = positions
                    if len(positions)!=4 or positions!=expected or positions[-1]>columns-2:
                        return False
            return True
        self.session.wait(valid,seconds=8)
        (self.output/f"screen-{columns}-sidebar-{sidebar}.txt").write_text(self.session.visible)
        return len(self.table_groups(columns,sidebar)[1])

    def check(self, key, title, function):
        print(f"{key} START {title}",flush=True)
        self.last_evidence = None
        try:
            evidence = function() or {}
            result = {"case":key,"title":title,"status":"PASS","evidence":evidence}
        except Exception as exc:
            result = {"case":key,"title":title,"status":"FAIL","error":f"{type(exc).__name__}: {exc}"}
            if self.last_evidence is not None:
                result["evidence"] = self.last_evidence
        self.results.append(result)
        (self.output/"results.json").write_text(json.dumps({"model":self.args.model,"effort":self.args.effort,"results":self.results},ensure_ascii=False,indent=2))
        print(f"{key} {result['status']} {result.get('error','')}",flush=True)

    def generate(self):
        self.submit("@A 请直接输出一个 Markdown 表格，不要开场、结语，也不要用代码围栏包住表格。三列：项目、当前实现、注意事项；六个数据行。"
                    "包含较长中文技术分析（每格15至25个汉字）、English、行内代码和粗体文字。第三列至少一个完整长URL："+URL+
                    "。不要调用任何工具，不读写文件，不创建子代理。")
        self.complete(probe=True)
        self.source = next(e["body"] for e in reversed(self.state()["entries"]) if e["sender"]=="A" and not e.get("communication"))
        assert URL in self.source,"model omitted or changed the requested long URL"
        assert "|" in self.source,"model did not produce a table"
        self.tables_fit(160)
        return {"source_chars":len(self.source),"streaming":self.streaming,
                "bold_cells":sum(cell.bold for row in self.session.screen.buffer.values() for cell in row.values())}

    def resize(self):
        self.session.send(b"\x02")
        self.tables_fit(160,True)
        self.session.resize(80,100)
        self.tables_fit(80,True)
        self.session.send(b"\x02")
        self.tables_fit(80)
        self.session.resize(50,100)
        self.tables_fit(50)
        self.session.resize(160,100)
        self.tables_fit(160)
        return {"tested_content_widths":[157,133,53,77,47]}

    def peer_table(self):
        self.submit("@A 请只用 Room send_message 让 B 制作 Markdown 表格，对比 Diffusion、Flow Matching、Autoregressive。"
                    "三列、三个数据行，单元格文字不超过20字，不要代码围栏。expect_reply=true，要求 B 用 Room send_message 的 reply_to 回传完整表格。"
                    "你收到后只给一句总结，不重复表格。不要修改文件，不创建子代理，等回信时结束当前 turn，不轮询。")
        self.complete()
        # A can finish its first turn before B is ready; allow the asynchronous reply.
        self.session.wait(lambda:any(e.get("communication",{}).get("transport")=="room" and e["sender"]=="B" and e["target"]=="A"
                                    for e in self.state()["entries"] if e.get("communication")),seconds=180)
        self.session.wait(lambda:"0 工作中" in self.session.visible,seconds=180)
        self.session.read(.15)
        reply = next(e for e in reversed(self.state()["entries"]) if e.get("communication") and e["sender"]=="B" and e["target"]=="A")
        assert "|" in reply["body"],"B did not return Markdown table content"
        assert reply["communication"].get("reply_to"),"B's reply has no source link"
        assert "@B → @A" in self.session.visible,"public response card is not visible"
        self.tables_fit(160)
        return {"message_id":reply["id"],"reply_to":reply["communication"]["reply_to"],"source":reply["body"]}

    def copy_original_table(self):
        # The first source table may now be above the viewport; select it before
        # adding more messages, while all of it is visible at the wide size.
        left,groups = self.table_groups(160,False)
        group = groups[-1]
        first,last = group[0][0],group[-1][0]
        right = max(x for x,cell in group[-1][1].items() if cell.data=="┘")
        self.copied.write_text("")
        self.session.send(f"\x1b[<0;{left+1};{first+1}M".encode())
        self.session.read(.05)
        self.session.send(f"\x1b[<32;{right+2};{last+1}M\x1b[<0;{right+2};{last+1}m".encode())
        self.session.wait(lambda:any(cell.bg=="415a74" for row in self.session.screen.buffer.values() for cell in row.values()))
        self.session.send(b"\x03")
        self.session.wait(lambda:bool(self.copied.read_text()))
        copied = self.copied.read_text()
        (self.output/"copied-table.txt").write_text(copied)
        self.session.send(b"\x1b")
        self.session.read(.1)
        source = next(e["body"] for e in self.state()["entries"] if e["sender"]=="A" and URL in e["body"])
        self.last_evidence = {"clipboard_is_private_capture":True,"stored_source_unchanged":source==self.source,
                              "original_url_contiguous_in_copy":URL in copied,"copied_has_drawn_borders":"┌" in copied and "│" in copied,
                              "copied_line_count":len(copied.splitlines())}
        assert source == self.source,"stored original Markdown changed"
        assert URL in copied,"rendered selection splits the original URL across table borders/newlines"
        assert not any(char in copied for char in "┌┬┐├┼┤└┴┘│"),"clipboard contains drawn table borders"
        return self.last_evidence

    def restart(self):
        before = self.state()
        identities = {a["name"]:a.get("thread_id") for a in before["agents"]}
        assert self.session.finish()==0
        (self.output/"terminal-before-restart.ansi").write_bytes(self.session.output)
        self.session.close()
        self.start(80)
        self.tables_fit(80)
        after = self.state()
        assert identities == {a["name"]:a.get("thread_id") for a in after["agents"]}
        assert [(e["id"],e["body"]) for e in before["entries"]]==[(e["id"],e["body"]) for e in after["entries"]]
        return {"thread_ids_preserved":True,"all_message_sources_unchanged":True}

    def close(self):
        if self.session:
            try:
                if self.session.process.poll() is None:
                    self.session.finish()
            finally:
                self.session.close()
        state = self.state()
        (self.output/"room-state.json").write_text(json.dumps(state,ensure_ascii=False,indent=2))
        lines = ["# Real-model UI transcript","",f"{self.args.model} / {self.args.effort}",""]
        for entry in state["entries"]:
            lines.extend([f"## {entry['sender']} → {entry.get('target','')} · {entry['state']}","",entry["body"],""])
            for block in entry.get("blocks",[]):
                if block["kind"]!="text":
                    lines.extend([f"### {block['kind']} · {block['title']}","","````text",block["text"],"````",""])
        (self.output/"chat.md").write_text("\n".join(lines))
        self.tmp.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model",default="gpt-6.1-sol")
    parser.add_argument("--effort",default="medium")
    parser.add_argument("--output",type=Path,default=ROOT/"artifacts/room-eval-ui")
    parser.add_argument("--replay-state",type=Path,help="Recheck resize/copy/restart using previously recorded real-model table output; no new model turns")
    args = parser.parse_args()
    ui = UI(args)
    try:
        ui.start()
        if not args.replay_state:
            ui.check("UI01","真实模型表格及流式显示",ui.generate)
        ui.check("UI02","侧栏、缩放及47列正文",ui.resize)
        ui.check("UI04","表格复制保真",ui.copy_original_table)
        if not args.replay_state:
            ui.check("UI03","成员互发真实Markdown表格",ui.peer_table)
        ui.check("UI05","重启后按新宽度重排",ui.restart)
    finally:
        ui.close()
    print(f"REPORT {ui.output/'results.json'}",flush=True)
    return int(any(result["status"] != "PASS" for result in ui.results))


if __name__=="__main__":
    raise SystemExit(main())
