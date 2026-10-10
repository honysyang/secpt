import urllib.request, urllib.parse, http.cookiejar, re

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
print("login path:", loginpath[:100])
s,b,h = req(IDP + loginpath)
print("login page status:", s)
print("cookies:", [(c.name,c.value) for c in cj])
print(b[:5000])
