import urllib.request, urllib.parse, http.cookiejar, re, random, string

IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"

suffix = ''.join(random.choice(string.ascii_lowercase) for _ in range(6))
username = "att" + suffix
email = "att%s@rois.team" % suffix

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
redir = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a): return None
nor = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NR)

def GET(url, opener=op):
    try:
        r = opener.open(urllib.request.Request(url), timeout=30)
        return r.status, r.geturl(), dict(r.headers), r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, url, dict(e.headers), e.read().decode(errors="replace")

def POST(url, data, opener=op):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type":"application/x-www-form-urlencoded"})
    try:
        r = opener.open(req, timeout=30)
        return r.status, r.geturl(), dict(r.headers), r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, url, dict(e.headers), e.read().decode(errors="replace")

# register a fresh user
data = {"username":username,"email":email,"password":"Passw0rd!","confirmPassword":"Passw0rd!",
        "type":"true","invitationCode":"","displayName":"A","department":"IT"}
st,_,h,b = POST(IDP + "/register", data)
print("register", st, h.get("Location"))

# Now GET idp /saml/sso with the SAMLRequest obtained from SP earlier (regenerate)
st,_,h,b = GET(SP + "/saml/login", nor)
loc = h.get("Location")
print("SP login loc:", loc[:120])
query = loc.split("?",1)[1]
st,url,h,b = GET(IDP + "/saml/sso?" + query)
print("IDP sso", st, url)
print(b[:1500])
