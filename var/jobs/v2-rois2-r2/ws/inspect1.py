import urllib.request, urllib.parse, http.cookiejar, re, html

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

SP = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"
IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect)
opener.addheaders = [('User-Agent','Mozilla/5.0')]

def req(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    r = urllib.request.Request(url, data=body)
    try:
        resp = opener.open(r, timeout=25)
        return resp.getcode(), dict(resp.headers), resp.read().decode('utf-8','replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf-8','replace')

# Inspect raw SP login Location
c, h, b = req(SP + "/saml/login")
raw = h.get("Location","")
print("RAW SP /saml/login Location:")
print(raw)
