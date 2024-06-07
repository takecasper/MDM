import json
import requests
import logging

class MosyleConnection:
    def get_jwt_token(self):
        login_url = f"{self.url}/login" 
        login_payload = {
            "email": self.username,
            "password": self.password
        }
        login_headers = {
            "accesstoken": self.api_key,
            'Content-Type': 'application/json'
        }
        response = requests.post(login_url, headers=login_headers, json=login_payload)
        if response.status_code == 200 and 'Authorization' in response.headers:
            return response.headers['Authorization']
        else:
            logging.error(f"Failed to get JWT token from Mosyle:\nHTTP Status Code: {response.status_code}\nResponse: {response.text}")
            raise ConnectionError("Failed to get JWT token")
        
    def __init__(self, url, api_key, username, password):
        self.s = requests.Session()
        self.url = url
        self.username = username
        self.password = password
        self.api_key = api_key
        self.jwt_token = self.get_jwt_token()
        self.headers = {
            "accesstoken": api_key,
            'Content-Type': 'application/json',
            'Authorization': self.jwt_token
        }
        payload = {
            "operation": "list",
            "options": {
                "os": "mac"
            }
        }

    def validate_request(self, request):
        prepped = request.prepare()
        # Add JWT token to the headers
        prepped.headers['Authorization'] = self.jwt_token
        response = self.s.send(prepped)
        if response.status_code != 200:
            logging.info(f"Failed to get devices from Mosyle:\nHTTP Status Code: {response.status_code}\nResponse: {response.text}")
            return response, False
        if json.loads(response.text)['status'] != "OK":
            logging.info(f"Failed to get devices from Mosyle:\nHTTP Status Code: {response.status_code}\nResponse: {response.text}")
            return response, False
        return response, True

    def get_devices(self, device_type, specific_columns=None):
        fail = 1
        page = 1
        payload = {
            "operation": "list",
            "options": {
                "os": device_type,
                "page": page
            }
        }
        if specific_columns is not None:
            payload['options']['specific_columns'] = specific_columns
        all_devices = []
        while fail > 0 and fail <= 3:
            request = requests.Request("POST", f"{self.url}/devices", headers=self.headers, json=payload)
            response, validated = self.validate_request(request)
            if not validated:
                fail = fail + 1
                continue
            mosyle_response = json.loads(response.text)['response'][0]
            if 'status' in mosyle_response and mosyle_response['status'] == "DEVICES_NOTFOUND":
                fail = 0
                break
            for device in mosyle_response['devices']:
                all_devices.append(device)
            payload['options']['page'] = payload['options']['page'] + 1
        return all_devices
    
    def update_devices(self, serial_number, provided_payload):
        fail = 1
        payload = {
            "operation": "update_device",
            "serialnumber": serial_number,
        }
        payload = payload | provided_payload
        while fail > 0 and fail <= 3:
            request = requests.Request("POST", f"{self.url}/devices", headers=self.headers, json=payload)
            response, validated = self.validate_request(request)
            if validated:
                break
            else:
                fail = fail + 1
                continue
        return fail < 4
            