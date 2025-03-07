from flask import Flask, jsonify, request, redirect
import os
import re
import requests
import subprocess
import signal
import json
import base64
import time

from config import ConfigManager, ConfigUci
from utils import get_ip_address, get_wifi_mac_address, get_bt_mac_address, get_device_id, get_uptime, get_load_avg, get_memory_usage, get_volume, set_volume
import const

from intents import intents

hostname = os.uname()[1]
speaker_ip = get_ip_address('wlan0')
app = Flask(__name__)

app.register_blueprint(intents)

config = ConfigManager(const.config_listener)
config_tts = ConfigManager(const.config_tts)
system_version = ConfigUci(const.mico_version)

def get_hass_token() -> str:
  token = config.HA_TOKEN
  if not token:
    return ""
  try:
    token_parts = token.split('.')
    if len(token_parts) != 3:
      raise ValueError('Invalid HA token format')

    payload = token_parts[1]
    payload += '=' * (4 - len(payload) % 4)  # Add padding if necessary
    decoded_payload = json.loads(base64.urlsafe_b64decode(payload).decode('utf-8'))

    if decoded_payload['exp'] < time.time():
      if config.HA_REFRESH_TOKEN:
        if home_assistant_refresh_token():
          token = config.HA_TOKEN
        else:
          raise ValueError('Failed to refresh token')
      else:
        raise ValueError('Token expired and no refresh token available')
    return token
  except Exception as e:
    raise ValueError(f'Failed to get HA token: {e}')

@app.route('/')
@app.route('/app.js')
def files():
  file = 'index.html' if request.path == '/' else request.path.lstrip('/')
  return app.send_static_file(file)

@app.get('/config')
def get_config():
  data = {
    'listener': config.to_dict(),
    'tts': config_tts.to_dict(),
  }
  return jsonify({'data': data})

@app.post('/config')
def set_config():
  """ Update config files and reload listener service """
  keys_accepted = ['ha_stt_provider', 'stt_language', 'tts_language', 'word']
  updated = False
  for key in keys_accepted:
    value = request.form.get(key, '').strip()
    if value:
      if config.set(key.upper(), value) is not True:
        updated = True

  if request.form.get('tts_language'):
    if config_tts.set("LANGUAGE", request.form.get('tts_language')) is not True:
      updated = True

  if updated:
    service_path = os.path.join(const.services_dir, 'listener')
    os.system(f'{service_path} reload')

  return redirect('/', code=302)

@app.get('/config/stt')
def get_stt_providers():
  """ Get all state entities from HA, and filter for STT ones only """
  try:
    token = get_hass_token()
  except ValueError:
    return jsonify({'error': 'Failed to get HA token, config missing?'}), 404

  url = f'{config.HA_URL}/api/states'
  headers = {
    'Authorization': f'Bearer {token}',
    'Content-Type': 'application/json',
  }

  req = requests.get(url, headers=headers)
  req.raise_for_status()

  providers = list()
  data = req.json()
  for entity in data:
    if entity['entity_id'].startswith('stt.'):
      entry = {"entity_id": entity['entity_id'], "name": entity['attributes'].get('friendly_name', entity['entity_id'])}
      providers.append(entry)

  return jsonify({'data': {'providers': providers, 'current': config.get('HA_STT_PROVIDER') or None}})

@app.get('/config/wakewords')
def get_wakewords():
  """ Get the wakewords from Porcupine, remove the file name, and only get the wakeword name """
  wakewords = []
  if os.path.exists(const.wakewords_porcupine) and os.path.isdir(const.wakewords_porcupine):
    wakewords = [f.replace('_raspberry-pi.ppn', '') for f in os.listdir(const.wakewords_porcupine) if f.endswith('.ppn')]
  return jsonify({'data': {'wakewords': wakewords, 'current': config.WORD or None}})

@app.get('/discover')
def info():
  """ Return Home Assistant instances found on the network via mDNS / avahi"""
  service_search_name = '_home-assistant._tcp'
  def parse_avahi_output(output):
    instances = list()
    for line in output.split('\n'):
      if service_search_name in line and 'IPv4' in line:
        parts = line.split(';')
        if len(parts) > 7:
          service_name = parts[3]
          txt_records = parts[9]
          txt_dict = {}
          for txt in re.findall(r'"(.*?)"', txt_records):
            if '=' in txt:
              key, val = txt.split('=', 1)
              if val == 'True':
                val = True
              if val == 'False':
                val = False
              txt_dict[key] = val
          instances.append(txt_dict)
    return instances

  try:
    result = subprocess.run(f'avahi-browse -rpt {service_search_name}'.split(' '), capture_output=True, text=True)
    if result.returncode == 0:
      instances = parse_avahi_output(result.stdout)
      return jsonify({'hostname': hostname, 'instances': instances})
    else:
      return jsonify({'hostname': hostname, 'instances': []}), 500
  except Exception as e:
    return jsonify({'hostname': hostname, 'error': str(e)}), 500

@app.get('/device/info')
def device_info():
  data = {
    'hostname': hostname,
    'ip': speaker_ip,
    'model': system_version.HARDWARE,
    'serial_number': get_device_id(),
    'wifi': get_wifi_mac_address(),
    'bluetooth': get_bt_mac_address(),
    'version': system_version.to_dict(),
    'volume': get_volume(),
    'uptime': get_uptime(),
    'load_avg': get_load_avg(),
    'memory_used': get_memory_usage(),
  }
  response = jsonify({'data': data})
  return response

@app.get('/volume')
def get_volume_level():
  """ Find available volume controls and return current volume """
  mixers = list()
  for mixer, name in const.volume_controls.items():
    volume = get_volume(mixer)
    if volume is not None:
      mixers.append({'name': name, 'mixer': mixer, 'volume': volume})
  return jsonify({'data': mixers})

@app.post('/volume')
@app.put('/volume')
def set_volume_level():
  """ Set volume for available volume controls """
  mixer = request.form.get('mixer') or request.args.get('mixer', 'mysoftvol')
  volume = request.form.get('volume') or request.args.get('volume', 0)
  try:
    volume = int(volume)
  except ValueError:
    return jsonify({'error': 'Invalid volume value'}), 400

  if volume < 0 or volume > 100:
    return jsonify({'error': 'Volume out of range'}), 400

  if mixer not in const.volume_controls.keys():
    return jsonify({'error': 'Invalid mixer name'}), 400

  try:
    result = set_volume(mixer, volume)
    if not result:
      raise
  except:
    return jsonify({'error': 'Failed to set volume'}), 500

  return jsonify({'message': 'Volume set successfully'})

@app.route('/mute')
@app.route('/unmute')
def manage_listener():
  action = 'stop' if request.path == '/mute' else 'start'
  silent = 'SILENT=1' if 'silent' in request.args else ''
  service = os.path.join(const.services_dir, 'listener')
  os.system(f'{silent} {service} {action}')
  return ""

@app.route('/wake')
def trigger_wake():
  process_name = '/usr/bin/porcupine'
  try:
    result = subprocess.run(['pgrep', '-x', process_name], capture_output=True, text=True)
    if result.returncode == 0:
      pid = result.stdout.strip()
      os.kill(int(pid), signal.SIGINT)
      return "", 200
    else:
      return "", 425
  except Exception as e:
    return jsonify({'error': str(e)}), 500

@app.post('/auth')
def home_assistant_auth():
  ha_url = request.form.get('url', '').rstrip('/')
  if not ha_url or not ha_url.startswith('http'):
    return jsonify({'error': 'Missing url parameter'}), 400

  if config.HA_URL == ha_url and config.HA_TOKEN and len(config.HA_TOKEN) > 30 and config.HA_AUTH_SETUP:
    return jsonify({'message': 'Instance already configured'})

  config.HA_URL = ha_url
  config.HA_TOKEN = "none"
  config.HA_AUTH_SETUP = False

  data = {
    'client_id': f'http://{speaker_ip}',
    'redirect_uri': f'http://{speaker_ip}/auth_callback',
  }

  query_params = '&'.join([f'{key}={value}'.replace(':','%3A').replace('/','%2F') for key, value in data.items()])
  return redirect(f'{ha_url}/auth/authorize?{query_params}', code=303)

# https://developers.home-assistant.io/docs/auth_api/
@app.get('/auth_callback')
def home_assistant_auth_callback():
  code = request.args.get('code')
  store_token = request.args.get('storeToken', 'false').lower() == 'true'
  state = request.args.get('state')

  if not code:
    return jsonify({'error': 'Missing code parameter'}), 400

  data = {
    'grant_type': 'authorization_code',
    'code': code,
    'client_id': f'http://{speaker_ip}',
  }

  headers = {
    'Content-Type': 'application/x-www-form-urlencoded'
  }

  ha_url = config.HA_URL
  if not ha_url:
    return jsonify({'error': 'Home Assistant URL not configured'}), 500

  req = requests.post(f'{ha_url}/auth/token', data=data, headers=headers)

  if req.status_code != 200:
    return jsonify({'error': 'Failed to get access token', 'code': req.status_code, 'response': req.json()}), 500

  token = req.json()
  if store_token:
    config.HA_TOKEN = token['access_token']
    config.HA_REFRESH_TOKEN = token['refresh_token']
    config.HA_AUTH_SETUP = True

  return jsonify({'message': 'Auth configured'})

def home_assistant_refresh_token():
  """ Call manually to trigger refresh token """
  ha_url = config.HA_URL
  if not ha_url:
    return False

  data = {
    'grant_type': 'refresh_token',
    'refresh_token': config.HA_REFRESH_TOKEN,
  }

  headers = {
    'Content-Type': 'application/x-www-form-urlencoded'
  }

  req = requests.post(f'{ha_url}/auth/token', data=data, headers=headers)

  if req.status_code != 200:
    return False

  token = req.json()
  config.HA_TOKEN = token['access_token']

  return True

@app.route('/services')
def list_services():
  """ List of system services available to manage, only via allowed list """
  files = [f for f in os.listdir(const.services_dir) if os.access(os.path.join(const.services_dir, f), os.X_OK) and f in const.services_allowed]
  return jsonify({'data': {'services': files}})

@app.route('/services/<service>/<action>')
def manage_service(service, action):
  action = action.lower().strip()
  service_path = os.path.join(const.services_dir, service)
  if not os.path.exists(service_path) or not os.access(service_path, os.X_OK):
    return jsonify({'error': 'Service not found or not executable'}), 404

  if service not in const.services_allowed:
    return jsonify({'error': 'Service not allowed'}), 403

  if action not in ['start', 'stop', 'restart']:
    return jsonify({'error': 'Invalid action'}), 400

  result = os.system(f'{service_path} {action}')
  if result != 0:
    return jsonify({'error': f'Failed to {action} service'}), 500

  return jsonify({'message': f'Service {service} {action}ed successfully'})

@app.get('/ir')
def send_infrared():
  """ Send IR RAW code """
  code = request.args.get('code')
  carrier = request.args.get('carrier')

  # TODO: check model
  if not hostname.lower().startswith('lx06'):
    return jsonify({'error': 'Infrared not supported on this model'}), 415

  if not code:
    return jsonify({'error': 'Missing code or carrier parameter'}), 400

  code = code.strip().replace('+', ' ').replace(' ', ',')
  if not re.match(r'^[0-9,-]+$', code):
    return jsonify({'error': 'Invalid code format'}), 400

  try:
    with open(const.lx06_infrared, 'w') as f:
      f.write(code)
  except IOError as e:
    return jsonify({'error': f'Failed to send infrared signal: {e}'}), 500

  return jsonify({}), 200

if __name__ == '__main__':
  app.run(debug=False, host='0.0.0.0', port=80)
