import json,sys
out=sys.argv[1]
legend={"entries":{"frontend":{"label":"caller (example/example.cpp)"},"backend":{"label":"factory the change is about (_mt and _st drawn as one)"},"cloud":{"label":"callee"}}}
C=lambda i,t,l,s,x,y,w=230,h=60,tag=None: dict({"id":i,"type":t,"label":l,"sublabel":s,"pos":[x,y],"size":[w,h]},**({"tag":tag} if tag else {}))
callers=[
 C("ex_basic","frontend","basic_example","example.cpp:121",40,40),
 C("ex_async","frontend","async_example","example.cpp:165",40,130),
 C("ex_replace","frontend","replace_default_logger_example","example.cpp:374",40,220,250),
 C("ex_daily","frontend","daily_example","example.cpp:134",40,330),
 C("ex_rot","frontend","rotating_example","example.cpp:128",40,730),
 C("handlers","cloud","file_event_handlers","ctor ()",860,340,200,200),
 C("reg_inst","cloud","details::registry::instance","registry& ()",860,880,200),
 C("reg_flush","cloud","details::registry::flush_all","void ()",860,980,200),
]
fam=[ # id, label, file, old line, new line, old risk mt, new risk mt, y
 ("basic","basic_logger_mt · _st","basic_file_sink.h",45,48,"0.79","0.75",40),
 ("daily","daily_logger_mt · _st","daily_file_sink.h",206,218,"0.76","0.72",250),
 ("dfmt","daily_logger_format_mt · _st","daily_file_sink.h",218,232,"0.70","0.67",450),
 ("rot","rotating_logger_mt · _st","rotating_file_sink.h",69,73,"0.76","0.72",650),
]
X=440
base=[C(f"{i}_old","backend",l,f"(…, handlers) · {f}:{a}",X,y,290,64,f"NodeRemoved ×2 · risk {ra}") for i,l,f,a,b,ra,rb,y in fam]
head=[C(f"{i}_new","backend",l,f"(…, handlers, unsigned long) · {f}:{b}",X,y+84,290,64,f"NodeAdded ×2 · risk {rb}") for i,l,f,a,b,ra,rb,y in fam]
head.append(C("flush_all","backend","spdlog::flush_all","void () · spdlog-inl.h:54",X,920,290,64,"NodeAdded · risk 0.67"))
def conns(sfx):
    cs=[(c,"basic") for c in ["ex_basic","ex_async","ex_replace"]]+[("ex_daily","daily"),("ex_rot","rot")]
    r=[{"id":f"{c}-{f}_{sfx}","from":c,"to":f"{f}_{sfx}"} for c,f in cs]
    r+=[{"id":f"{f[0]}_{sfx}-handlers","from":f"{f[0]}_{sfx}","to":"handlers"} for f in fam]
    return r
hc=conns("new")+[{"id":"flush-inst","from":"flush_all","to":"reg_inst"},{"id":"flush-flush","from":"flush_all","to":"reg_flush"}]
def doc(t,o,c,k): return {"schema_version":1,"diagram_type":"architecture","meta":{"title":t,"output":o,"quality_profile":"showcase","legend":legend},"components":c,"connections":k}
json.dump(doc("spdlog · component 0 · base (bfde7d3711d5)",out+"/base.html",callers+base,conns("old")),open("base.json","w"),indent=1)
json.dump(doc("spdlog · component 0 · head (d6c93f38a08b)",out+"/head.html",callers+head,hc),open("head.json","w"),indent=1)
