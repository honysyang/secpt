import urllib.request, urllib.error

SP = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

op = urllib.request.build_opener(NoRedirect)
try:
    r = op.open(SP + "/saml/login", timeout=30)
    print("status", r.status)
    print(dict(r.headers))
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
    print("Location:", e.headers.get('Location'))
    print("Set-Cookie:", e.headers.get('Set-Cookie'))
