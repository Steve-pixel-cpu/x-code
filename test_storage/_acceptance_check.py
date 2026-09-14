"""验收脚本 — 跑完即删，不属于项目代码。覆盖 6 项验收标准。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # test_storage 的上一级 = 项目根
from storage import SessionStore
from models import Message, TextContentBlock

def assistant_text(s):
    return Message(role="assistant", content=[TextContentBlock(text=s)])

tmp = Path(tempfile.mkdtemp(prefix="xcode_accept_"))
store = SessionStore(tmp)
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "|", name, ("-- " + str(extra) if extra else ""))


# ---- T1 往返 + leaf uuid ----
u1 = store.save_message("s1", Message.user_text("你好"), None)
u2 = store.save_message("s1", Message.tool_use("t1", "bash", "ls"), u1)
u3 = store.save_message("s1", Message.tool_result("t1", "bash", "ok", False), u2)
msgs, leaf = store.load_session("s1")
check("T1 往返: 恢复3条消息", len(msgs) == 3, f"got {len(msgs)}")
check("T1 leaf == 最后一条的uuid", leaf == u3, f"leaf={leaf} u3={u3}")
check("T1 文本未丢失", bool(msgs) and msgs[0].content[0].text == "你好")
check("T1 子类正确还原", type(msgs[1].content[0]).__name__ == "ToolContentBlock",
      type(msgs[1].content[0]).__name__)

raw = (tmp / "s1.jsonl").read_text(encoding="utf-8")
check("T1b 中文在文件里肉眼可读", "你好" in raw)

# ---- T2 用返回的 leaf 接续对话（resume 不断链）----
u4 = store.save_message("s1", Message.user_text("继续"), leaf)
msgs2, leaf2 = store.load_session("s1")
check("T2 resume接续: 恢复4条", len(msgs2) == 4, f"got {len(msgs2)}")
check("T2 新leaf正确", leaf2 == u4)

# ---- T3 崩溃安全: 半行 + 合法JSON但缺字段 ----
ca = store.save_message("crash", Message.user_text("one"), None)
cb = store.save_message("crash", assistant_text("two"), ca)
cp = tmp / "crash.jsonl"
with open(cp, "a", encoding="utf-8") as f:
    f.write('{"uuid": "abc", "mess')      # 崩溃半行
with open(cp, "a", encoding="utf-8") as f:
    f.write('\n{"uuid": "x"}\n')          # 合法JSON但缺字段
print("--- 期望看到两条带行号的警告 ---")
msgs3, leaf3 = store.load_session("crash")
check("T3 崩溃安全: 仍恢复2条", len(msgs3) == 2, f"got {len(msgs3)}")
check("T3 leaf指向最后完整消息", leaf3 == cb)

# ---- T4 fork 毕业考: a1<-b2<-c3, a1<-d4 ----
def w(uid, pid, text):
    e = {"uuid": uid, "parent_uuid": pid,
         "message": {"role": "user", "content": [{"type": "text", "text": text}]},
         "timestamp": "t"}
    with open(tmp / "fork.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")

w("aaa", None, "A"); w("bbb", "aaa", "B"); w("ccc", "bbb", "C"); w("ddd", "aaa", "D")
msgsf, leaff = store.load_session("fork")
texts = [m.content[0].text for m in msgsf]
check("T4 fork: 恢复最新分支 [A, D]", texts == ["A", "D"], f"got {texts}")
check("T4 fork: leaf == ddd", leaff == "ddd", f"got {leaff}")

# ---- T5 中断检测 ----
check("T5 assistant结尾 -> None", store.detect_interruption("crash") is None,
      store.detect_interruption("crash"))
store.save_message("iru", Message.user_text("q"), None)
check("T5 user结尾 -> 'user'", store.detect_interruption("iru") == "user",
      store.detect_interruption("iru"))
it1 = store.save_message("irt", Message.user_text("q"), None)
store.save_message("irt", Message.tool_result("t9", "bash", "out", False), it1)
check("T5 tool结尾 -> 'tool'", store.detect_interruption("irt") == "tool",
      store.detect_interruption("irt"))

# ---- T6 空会话 + 懒物化 ----
m0, l0 = store.load_session("nope")
check("T6 空会话 -> ([], None)", m0 == [] and l0 is None)

tmp2 = Path(tempfile.mkdtemp()) / "sub"
s2 = SessionStore(tmp2)
s2.load_session("whatever")
check("T6 懒物化: 只读操作不建目录", not tmp2.exists(),
      "load_session 在全新store上就把目录建出来了" if tmp2.exists() else "")

print()
passed = sum(1 for _, ok in results if ok)
print(f"=== 结果: {passed}/{len(results)} PASS ===")
for name, ok in results:
    if not ok:
        print("  未通过:", name)
