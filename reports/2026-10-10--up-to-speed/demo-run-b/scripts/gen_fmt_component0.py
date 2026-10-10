import json,sys,copy
out=sys.argv[1]
legend={"entries":{"frontend":{"label":"caller (library entry point)"},"external":{"label":"caller (test)"},"backend":{"label":"function the change is about"},"cloud":{"label":"callee"}}}
C=lambda i,t,l,s,x,y,w=210,h=58,tag=None: dict({"id":i,"type":t,"label":l,"sublabel":s,"pos":[x,y],"size":[w,h]},**({"tag":tag} if tag else {}))
common=[
 C("print","frontend","fmt::print","(std::ostream&, fstring, T&&...)",40,40),
 C("vprint_c","frontend","fmt::vprint","(std::ostream&, string_view, args)",40,130),
 C("vprint_w","frontend","fmt::vprint (wchar_t)","(std::wostream&, wstring_view, args)",40,220),
 C("t_write","external","test: write_to_ostream","ostream-test.cc:127",40,310),
 C("t_max","external","test: write_to_ostream_max_size","ostream-test.cc:131 · body changed",40,410,230,80),
 C("max_value","cloud","detail::max_value","T ()",800,150,190),
 C("to_unsigned","cloud","detail::to_unsigned","make_unsigned<T> (T)",800,240,190),
 C("buf_ctor","cloud","detail::buffer::buffer","(grow_fn, T*, size, capacity)",800,370,190),
 C("mem_buf","cloud","basic_memory_buffer","ctor (const Allocator&)",800,460,190),
 C("sv_ctor","cloud","basic_string_view","ctor (const Char*)",800,550,190),
 C("buf_data","cloud","detail::buffer::data","T* ()",800,640,190),
 C("ice","cloud","detail::is_constant_evaluated","bool (bool)",800,763,190),
]
base_only=[
 C("wb_old","backend","detail::write_buffer","(ostream&, buffer&) · ostream.h:55",420,90,250,130,"NodeRemoved · risk 0.86"),
]
head_only=[
 C("wb_new","backend","detail::write_buffer","(ostream&, buffer&, unsigned long) · ostream.h:56",420,270,250,130,"NodeAdded · risk 0.82"),
 C("fill_n","backend","detail::fill_n","(appender, unsigned long, T) · format.h:548",420,760,240,64,"NodeAdded · risk 0.72"),
]
def calls(wb):
    return [{"id":f"{c}-{wb}","from":c,"to":wb} for c in ["print","vprint_c","vprint_w","t_write","t_max"]] + \
           [{"id":f"{wb}-{t}","from":wb,"to":t} for t in ["max_value","to_unsigned"]]
base_conn=calls("wb_old")+[{"id":"t_max-max_value","from":"t_max","to":"max_value"},{"id":"t_max-to_unsigned","from":"t_max","to":"to_unsigned"},{"id":"t_max-buf_ctor","from":"t_max","to":"buf_ctor"}]
head_conn=calls("wb_new")+[{"id":"t_max-mem_buf","from":"t_max","to":"mem_buf"},{"id":"t_max-sv_ctor","from":"t_max","to":"sv_ctor"},{"id":"t_max-buf_data","from":"t_max","to":"buf_data"},{"id":"fill_n-ice","from":"fill_n","to":"ice"}]
cards=[
 {"dot":"rose","title":"What the tool reported","items":["write_buffer(os, buf) removed, score 0.86","write_buffer(os, buf, unsigned long) added, score 0.82","Every caller edge removed and re-added: 8 consequences nested under each","28 events in component 0; 3 top-level"]},
 {"dot":"amber","title":"What it most likely is","items":["One signature change: a size parameter was added","Tier-1 identity only (ARCH-001 §13), so a changed signature is a new symbol","Tiers 2–4 would pair these and collapse 18 events into roughly one"]},
 {"dot":"slate","title":"Left out of the drawing","items":["3 namespace detail ‘contains’ edges (1 removed, 2 added)","The text view prints them as ‘detail -> write_buffer’, which reads like a call"]},
]
def doc(title,outp,comps,conns):
    return {"schema_version":1,"diagram_type":"architecture","meta":{"title":title,"output":outp,"quality_profile":"showcase","legend":legend},"components":comps,"connections":conns,"cards":cards}
json.dump(doc("fmt · component 0 · base (cdc27f91a007)",out+"/base.html",common+base_only,base_conn),open("base.json","w"),indent=1)
json.dump(doc("fmt · component 0 · head (35c58f084e7c)",out+"/head.html",common+head_only,head_conn),open("head.json","w"),indent=1)
