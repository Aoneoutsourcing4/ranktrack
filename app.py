"""RankTracker Lite: same-origin SerpAPI proxy, Python standard library."""
import json, os, re, hashlib
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT=Path(__file__).resolve().parent
COUNTRIES={'us':('United States','google.com'),'ca':('Canada','google.ca'),'uk':('United Kingdom','google.co.uk'),'au':('Australia','google.com.au'),'ie':('Ireland','google.ie')}
class APIError(Exception):
    def __init__(self,message,code=502): super().__init__(message); self.code=code

def upstream(path,params):
    key=params.get('api_key','')
    def clean(s): return str(s).replace(key,'[redacted]') if key else str(s)
    try:
        with urlopen(Request('https://serpapi.com/'+path+'?'+urlencode(params),headers={'Accept':'application/json','User-Agent':'RankTrackerLite/2.0'}),timeout=65) as response:
            data=json.load(response)
    except HTTPError as e:
        try: msg=json.loads(e.read()).get('error','HTTP '+str(e.code))
        except Exception: msg='HTTP '+str(e.code)
        raise APIError(clean(msg),e.code)
    except (TimeoutError,URLError,ValueError): raise APIError('SerpAPI timed out or returned invalid JSON. Retry later.',504)
    if not isinstance(data,dict): raise APIError('Unexpected API response.')
    if data.get('error'): raise APIError(clean(data['error']))
    if path=='search.json' and (data.get('search_metadata') or {}).get('status') not in ('Success',None):
        raise APIError('Search not completed. Retry later.')
    return data

def host(value):
    value=str(value).strip(); p=urlsplit(value if '://' in value else 'https://'+value)
    if p.scheme not in ('http','https') or not p.hostname or '.' not in p.hostname or p.username or p.password or re.search(r'\s',value): raise ValueError('Enter a valid website domain.')
    return p.hostname.lower().rstrip('.').removeprefix('www.')

def text(p,k,maximum):
    v=str(p.get(k,'')).strip()
    if len(v)>maximum: raise ValueError(k+' is too long.')
    return v

def key_of(p):
    k=text(p,'key',256)
    if not k: raise ValueError('Enter your SerpAPI key.')
    return k

def account(p):
    a=upstream('account.json',{'api_key':key_of(p)})
    return {'plan':a.get('plan_name'),'remaining':a.get('total_searches_left',a.get('plan_searches_left'))}

def page(p):
    key=key_of(p); target=host(p.get('domain','')); keyword=text(p,'keyword',500); country=p.get('country'); device=p.get('device','desktop'); location=text(p,'location',250); offset=p.get('offset',0)
    if not keyword or country not in COUNTRIES or device not in ('desktop','mobile') or type(offset) is not int or offset not in range(0,100,10): raise ValueError('Invalid search settings.')
    name,google=COUNTRIES[country]
    params={'engine':'google','q':keyword,'api_key':key,'gl':country,'google_domain':google,'hl':'en','device':device,'start':offset}
    params['location']=(location if location.lower().endswith(name.lower()) else location+', '+name) if location else name
    if p.get('web') is True: params['udm']='web'
    if p.get('fresh') is True: params['no_cache']='true'
    data=upstream('search.json',params)
    items=data.get('organic_results') or []
    sid=(data.get('search_metadata') or {}).get('id','')
    if not items: return {'state':'inconclusive','message':'No organic results returned. Retry or try Web-only mode.','search_id':sid,'count':0}
    positions=[x.get('position') for x in items if type(x.get('position')) is int and x['position']>0]
    absolute=bool(offset and positions and min(positions)>offset)
    matches=[]; scanned=0; links=[]
    for i,item in enumerate(items,1):
        raw=item.get('position'); pos=raw if type(raw) is int and raw>0 else i
        pos=pos if absolute else offset+pos
        scanned=max(scanned,pos); link=item.get('link',''); links.append(link)
        try: h=host(link)
        except (ValueError,TypeError): continue
        if h==target or h.endswith('.'+target): matches.append({'rank':pos,'url':link,'title':item.get('title','')})
    matches.sort(key=lambda m:m['rank'])
    pagination=data.get('serpapi_pagination') or {}; fallback=data.get('pagination') or {}
    nxt=pagination.get('next') or pagination.get('next_link') or fallback.get('next')
    next_offset=None
    if nxt:
        try:
            n=int(parse_qs(urlsplit(nxt).query)['start'][0]); next_offset=n if n>offset and n in range(0,100,10) else None
        except (ValueError,KeyError,TypeError): pass
    if next_offset is None and len(items)>=10 and offset<90: next_offset=offset+10
    return {'state':'ok','match':matches[0] if matches else None,'scanned':scanned,'count':len(items),'next_offset':next_offset,'search_id':sid,'signature':hashlib.sha256(json.dumps(links).encode()).hexdigest(),'location':(data.get('search_parameters') or {}).get('location_used',params['location'])}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,obj,status=200,html=False):
        b=obj if html else json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path=='/': self.send((ROOT/'index.html').read_bytes(),html=True)
        elif self.path=='/health': self.send({'ok':True})
        else: self.send({'error':'Not found'},404)
    def do_POST(self):
        origin=self.headers.get('Origin','')
        if (origin and urlsplit(origin).netloc!=self.headers.get('Host','')) or self.headers.get('Content-Type','').split(';')[0]!='application/json':
            return self.send({'error':'Use the tool from its own website.'},403)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=16000: raise ValueError('Invalid request size.')
            p=json.loads(self.rfile.read(size))
            if not isinstance(p,dict): raise ValueError('Invalid request.')
            if self.path=='/api/account': return self.send(account(p))
            if self.path=='/api/page': return self.send(page(p))
            return self.send({'error':'Not found'},404)
        except (ValueError,TypeError,KeyError) as e: self.send({'error':str(e)},400)
        except APIError as e: self.send({'error':str(e)},e.code if 400<=e.code<=599 else 502)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception: self.send({'error':'Unexpected server error.'},500)

if __name__=='__main__':
    cloud='PORT' in os.environ; port=int(os.environ.get('PORT','8765'))
    server=ThreadingHTTPServer(('0.0.0.0' if cloud else '127.0.0.1',port),Handler)
    print(f'RankTracker Lite listening on port {port}',flush=True)
    server.serve_forever()
