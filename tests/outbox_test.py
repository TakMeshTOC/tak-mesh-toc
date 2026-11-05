import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/bridge", PKG + "/chat", PKG + "/dashboard"]
import sys, time, socket, json, os
sys.path.insert(0, PKG + "/chat")
import deck_chat as dc
res=[]
def check(n,c,x=""):
    res.append(c); print(("PASS  " if c else "FAIL  ")+n+("" if c else "  "+str(x)))
os.makedirs("/tmp/mmb_chat", exist_ok=True); H="/tmp/mmb_chat/ob_hist.jsonl"
if os.path.exists(H): os.remove(H)
cfg=dc.load_cfg(None)
for k,v in {"MESH_LOCAL_ADDR":"auto","MESH_IFACE":"nope0","HISTORY_FILE":H,"OUTBOX_HOLD_SECONDS":"2","CHAT_GROUP":"224.10.10.7:17077","UID":"DECK-ob"}.items(): cfg[k]=v
rx=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); rx.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
rx.bind(("224.10.10.7",17077)); rx.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,socket.inet_aton("224.10.10.7")+socket.inet_aton("127.0.0.1")); rx.settimeout(0.5)
def got():
    out=[]
    try:
        while True: out.append(rx.recvfrom(65535)[0])
    except socket.timeout: return out
c=dc.Chat(cfg)
r1=c.send("first while down"); r2=c.send("second while down")
check("mesh down -> accepted but queued", r1["ok"] and r1["status"]=="queued" and r2["status"]=="queued", (r1,r2))
check("queued note explains why", "nope0" in r1["note"], r1)
check("info counts 2 waiting", {k:c.info()["outbox"][k] for k in ("waiting","held")}=={"waiting":2,"held":0}, c.info()["outbox"])
check("nothing transmitted yet", got()==[])
cfg["MESH_IFACE"]="lo"            # node plugged back in
c.flush(); pk=got()
texts=[dc.parse_chat(p)["text"] for p in pk]
check("mesh back -> both sent in order", texts==["first while down","second while down"], texts)
check("statuses now sent, outbox empty", all(m["status"]=="sent" for m in c.msgs) and not c.outbox)
# hold: down again, wait past hold time
cfg["MESH_IFACE"]="nope0"
r3=c.send("old news"); time.sleep(2.3)
cfg["MESH_IFACE"]="lo"
r4=c.send("fresh message")
c.flush(); pk=got(); texts=[dc.parse_chat(p)["text"] for p in pk]
m3=c.by_id[r3["id"]]
check("old queued message is HELD, not sent", m3["status"]=="held" and "old news" not in " ".join(texts), (m3["status"],texts))
check("held message does not block newer one", texts==["fresh message"], texts)
check("info shows 1 held", {k:c.info()["outbox"][k] for k in ("waiting","held")}=={"waiting":1,"held":1}, c.info()["outbox"])
a=c.outbox_action(r3["id"],"send"); pk=got(); texts=[dc.parse_chat(p)["text"] for p in pk]
check("confirm SEND transmits it, marked as late", a["ok"] and len(texts)==1 and texts[0].startswith("[queued ") and texts[0].endswith("old news"), (a,texts))
# drop
cfg["MESH_IFACE"]="nope0"; r5=c.send("drop me"); a=c.outbox_action(r5["id"],"drop")
cfg["MESH_IFACE"]="lo"; c.flush()
check("DROP removes it, never sent", a["ok"] and c.by_id[r5["id"]]["status"]=="dropped" and not any(b"drop me" in p for p in got()))
# restart with something queued -> comes back HELD
cfg["MESH_IFACE"]="nope0"; r6=c.send("queued at shutdown")
c2=dc.Chat(cfg)
m6=c2.by_id[r6["id"]]
check("after restart a queued message comes back HELD", m6["status"]=="held" and m6 in c2.outbox, m6.get("status"))
check("after restart sent/dropped keep their status", c2.by_id[r1["id"]]["status"]=="sent" and c2.by_id[r5["id"]]["status"]=="dropped")
check("export marks unsent messages", "NOT SENT: dropped" in c2.export_text() and "NOT SENT: held" in c2.export_text())

# ---------------- the sender itself failing shows on the chat screen; a bad packet is counted, not hidden
import threading, types
check("no sender failure: no STUCK", c.info()["outbox"]["error"] == "")
real_time = dc.time
class FastTime(types.ModuleType):
    def __getattr__(self, k): return getattr(real_time, k)
    def sleep(self, s): real_time.sleep(0.05)
dc.time = FastTime("time")
c3 = dc.Chat(cfg); c3.send("stuck one")
real_flush = c3.flush
def boom(): raise KeyError("status")
c3.flush = boom
threading.Thread(target=dc.outbox_worker, args=(c3,), daemon=True).start()
real_time.sleep(0.4)
ob = c3.info()["outbox"]
check("sender fails: the chat screen gets the reason (OUTBOX n STUCK; before: a journal line only)", ob["error"] == "KeyError: 'status'" and ob["waiting"] >= 1, ob)
c3.flush = real_flush
real_time.sleep(0.3)
check("...works again: STUCK clears", c3.info()["outbox"]["error"] == "", c3.info()["outbox"])
page = open(PKG + "/chat/index.html").read()
check("chat screen: STUCK pill (red) with the reason in its tooltip", 'ob.error) pill("pout", "bad", "OUTBOX " + ob.waiting + " · STUCK"' in page)
lo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); lo.bind(("127.0.0.1", 0))
grp = ("224.10.10.9", 17079)
c3.local_addr = lambda: "127.0.0.1"
def bad_handle(data, addr): raise ValueError("bad xml")
threading.Thread(target=dc.group_listener, args=(c3, grp, bad_handle, "test_flag", "test chat"), daemon=True).start()
real_time.sleep(0.5)
tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
for _ in range(3): tx.sendto(b"<event/>", grp)
real_time.sleep(0.5)
bp = c3.info()["bad_packets"]
check("a packet that breaks the handler is counted with the reason (before: debug log, invisible)", bp["count"] == 3 and "ValueError: bad xml" in bp["last"], bp)
dc.time = real_time
print("%d/%d passed"%(sum(res),len(res)))
sys.exit(0 if all(res) else 1)
