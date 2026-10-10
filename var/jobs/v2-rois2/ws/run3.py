import urllib.request, urllib.parse

SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect)
opener.addheaders = [('User-Agent','Mozilla/5.0')]

def req(url):
    try:
        resp = opener.open(url, timeout=30)
        return resp.status, resp.read().decode('utf-8','replace'), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8','replace'), dict(e.headers)

for path in ["/", "/saml/login", "/saml/metadata", "/admin"]:
    s,b,h = req(SP+path)
    print("=====", path, s)
    print("Location:", h.get('Location'))
    print(b[:600])
    print()
