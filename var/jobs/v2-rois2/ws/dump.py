data = open('src/auth/sp-flag/saml2/validator.py','rb').read().decode()
open('validator_tail.txt','w').write(data[7300:])
open('parser_dump.txt','w').write(open('src/auth/sp-flag/saml2/parser.py','rb').read().decode())
print("done", len(data))
