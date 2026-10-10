import re
s = open('attack.py').read()
old = 'nexturl = fix(hdrs.get(\'Location\') or \'\')'
new = ('loc2 = hdrs.get("Location") or ""\n'
       'nexturl = fix(loc2) if loc2.startswith("http") else IDP + loc2')
s = s.replace(old, new)
open('attack.py','w').write(s)
print("patched")
