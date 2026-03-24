import json
import uuid
import sys
import os

TOKEN_FILE = "/etc/mosquitto/provisioning_tokens.json"

# 1. 스크립트 실행 시 전달된 게이트웨이 ID를 가져옵니다.
if len(sys.argv) < 2:
    print(f"사용법: python {sys.argv[0]} [새로운_게이트웨이_ID]")
    sys.exit(1)

new_gateway_id = sys.argv[1]

# 2. 토큰 파일이 없으면 새로 만들고, 있으면 읽어옵니다.
if os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, 'r') as f:
        tokens = json.load(f)
else:
    tokens = {}

# 3. 이미 등록된 ID인지 확인합니다.
if new_gateway_id in tokens:
    print(f"오류: '{new_gateway_id}'는 이미 등록된 ID입니다.")
    sys.exit(1)

# 4. 새로운 UUID 토큰을 생성합니다.
#new_token = str(uuid.uuid4())

# 5. 새로운 게이트웨이 ID와 토큰을 추가합니다.
#tokens[new_gateway_id] = new_token

# 6. 변경된 내용을 파일에 다시 저장합니다.
#with open(TOKEN_FILE, 'w') as f:
 #   json.dump(tokens, f, indent=4)

print("✅ 성공적으로 토큰을 발급하고 저장했습니다.")
print(f"   게이트웨이 ID: {new_gateway_id}")
#print(f"   발급된 토큰: {new_token}")
