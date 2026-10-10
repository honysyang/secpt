import urllib.request, urllib.parse, http.cookiejar, re, html

SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"
IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(cj))
opener.addheaders = [('User-Agent','Mozilla/5.0')]

def req(url, data=None, method=None):
    if data is not None and isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    r = urllib.request.Request(url, data=data, method=method or ('POST' if data is not None else 'GET'))
    try:
        resp = opener.open(r, timeout=30)
        return resp.status, resp.read().decode('utf-8','replace'), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8','replace'), dict(e.headers)

s,b,h = req(SP + "/saml/login")
loc = h.get('Location').replace("http://127.0.0.1:80", IDP)
s,b,h = req(loc)
loginpath = h.get('Location')
s,b,h = req(IDP + loginpath)
samlRequest = re.search(r'name="samlRequest" value="([^"]*)"', b).group(1)
relayState = re.search(r'name="relayState" value="([^"]*)"', b).group(1)
s,b,h = req(IDP + "/login", data={'username':'admin','password':'RCTF2025','samlRequest':samlRequest,'relayState':relayState})
sso = h.get('Location')
if sso.startswith('/'):
    sso = IDP + sso
s,b,h = req(sso)
print("SSO status:", s, "loc:", h.get('Location'))
print("BODY len:", len(b))
print(b[:4000])
