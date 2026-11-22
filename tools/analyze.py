import json,gzip,re,sys
from pathlib import Path
ix=json.load(gzip.open('.build/out/index.json.gz','rt'))
T=json.load(open('sample/tests.json'))['tests']
num={t['id']:t['number'] for t in T}; dur={t['number']:round(t['seconds']*1000) for t in T}
idx2num=[num.get(t) for t in ix['tests']]
ans=json.load(open('sample/answers.json'))['commits']
def S(i): return {idx2num[x] for x in ix['sets'][i] if idx2num[x] is not None}
def hunks(text):
    out={}
    for block in text.split('diff --git ')[1:]:
        lines=block.splitlines()
        path=lines[0].split()[0][2:]
        ch=[]; old=None
        for l in lines:
            m=re.match(r'@@ -(\d+)',l)
            if m: old=int(m[1]); continue
            if old is None: continue
            if l.startswith('-') and not l.startswith('---'): ch.append(('-',old)); old+=1
            elif l.startswith('+') and not l.startswith('+++'): ch.append(('+',old))
            elif l.startswith(' '): old+=1
        out[path]=ch
    return out
for c in sorted(ans):
    f=set(ans[c]['failing'])
    h=hunks(open(f'sample/commits/{c}.patch').read())
    cand=set(); info=[]
    for p,ch in h.items():
        fl=ix['files'].get(p,{})
        for k,n in ch:
            ln=[n] if k=='-' else [n-1,n]
            s=set()
            for x in ln:
                if str(x) in fl: s|=S(fl[str(x)])
            imp=any(x in ix['import_lines'].get(p,[]) for x in ln)
            info.append((p.split('/')[-1],k,n,len(s),imp))
            cand|=s
        if p in ix['opens']: cand|=S(ix['opens'][p]); info.append(('OPEN',p))
    miss=f-cand
    print(c, ans[c]['kind'], 'fail',len(f),'cand',len(cand), 'candsec',round(sum(dur[x] for x in cand)/1000,1),'MISS',len(miss), info)
