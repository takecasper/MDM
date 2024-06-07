#!/bin/python3

import json
import os
import requests
import time
import configparser
import argparse
import logging
import mosyle_api
import subprocess

# Set us up for using runtime arguments by defining them.
runtimeargs = argparse.ArgumentParser()
runtimeargs.add_argument("--mdm", help="Sets the MDM system you'll be using, either Mosyle or Mosyle")
runtimeargs.add_argument("-v", "--verbose", help="Sets the logging level to INFO and gives you a better idea of what the script is doing.", action="store_true")
runtimeargs.add_argument("--dryrun", help="This checks your config and tries to contact both the MosylePro and Snipe-it instances, but exits before updating or syncing any assets.", action="store_true")
runtimeargs.add_argument("-d", "--debug", help="Sets logging to include additional DEBUG messages.", action="store_true")
runtimeargs.add_argument('--do_not_verify_ssl', help="Skips SSL verification for all requests. Helpful when you use self-signed certificate.", action="store_false")
runtimeargs.add_argument("-r", "--ratelimited", help="Puts a half second delay between Snipe IT API calls to adhere to the standard 120/minute rate limit", action="store_true")
user_opts = runtimeargs.add_mutually_exclusive_group()
user_opts.add_argument("-u", "--users", help="Checks out the item to the current user in Mosyle if it's not already deployed", action="store_true")
user_opts.add_argument("-ui", "--users_inverse", help="Checks out the item to the current user in Mosyle if it's already deployed", action="store_true")
user_opts.add_argument("-uf", "--users_force", help="Checks out the item to the user specified in Mosyle no matter what", action="store_true")
type_opts = runtimeargs.add_mutually_exclusive_group()
type_opts.add_argument("-m", "--mobiles", help="Runs mobiles only", action="store_true")
type_opts.add_argument("-c", "--computers", help="Runs computers only", action="store_true")
user_args = runtimeargs.parse_args()

# Notify users they're going to get a wall of text in verbose mode.
if user_args.verbose:
    logging.basicConfig(level=logging.INFO)
elif user_args.debug:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.WARNING)

# Notify users if we're doing a dry run.
if user_args.dryrun:
    print("Dryrun: Starting mosyle2snipe with a dry run where no assets will be updated.")

# Find a valid settings.conf file.
logging.info("Searching for a valid settings.conf file.")
config = configparser.ConfigParser()
logging.debug("Checking for a settings.conf in /opt/mosyle2snipe ...")
config.read("/opt/mosyle2snipe/settings.conf")
if 'snipe-it' not in set(config):
    logging.debug("No valid config found in: /opt Checking for a settings.conf in /etc/mosyle2snipe ...")
    config.read('/etc/mosyle2snipe/settings.conf')
if 'snipe-it' not in set(config):
    logging.debug("No valid config found in /etc Checking for a settings.conf in current directory ...")
    config.read("settings.conf")
if 'snipe-it' not in set(config):
    logging.debug("No valid config found in current folder.") 
    logging.error("No valid settings.conf was found. We'll need to quit while you figure out where the settings are at. You can check the README for valid locations.")
    raise SystemExit("Error: No valid settings.conf - Exiting.")

logging.info("Great, we found a settings file. Let's get started by parsing all of the settings.")

#Signin 1Password and grab session token
for i in range(3):
    try:
        op_signin_command = ["op", "signin", "--raw"]
        session_token = subprocess.check_output(op_signin_command, text=True).strip()
        break
    except subprocess.CalledProcessError as e:
        print(f"Sign in failed. {3-i-1} attempts remaining.")
else:
    print("Failed to sign in after 3 attempts. Script terminated!")
    exit()

mosyle_api_key_ref = config.get("mosyle", "api_key")
mosyle_username_ref = config.get("mosyle", "username")
mosyle_password_ref = config.get("mosyle", "password")

mosyle_api_key = subprocess.check_output(["op", "read", mosyle_api_key_ref, "--session", session_token], text=True).strip()
mosyle_username = subprocess.check_output(["op", "read", mosyle_username_ref, "--session", session_token], text=True).strip()
mosyle_password = subprocess.check_output(["op", "read", mosyle_password_ref, "--session", session_token], text=True).strip()

mosyle = mosyle_api.MosyleConnection(
    config['mosyle']['url'],
    mosyle_api_key,
    mosyle_username,
    mosyle_password
)

snipe_base = config['snipe-it']['url']
logging.info("The configured Snipe-IT base url is: {}".format(snipe_base))
snipeit_api_key_ref = config.get("snipe-it", "apikey")
apiKey = subprocess.check_output(["op", "read", snipeit_api_key_ref, "--session", session_token], text=True).strip()
logging.debug("The API key you provided for Snipe is: {}".format(apiKey))
defaultStatus = config['snipe-it']['defaultStatus']
logging.info("The default status we'll be setting updated computer to is: {} (I sure hope this is a number or something is probably wrong)".format(defaultStatus))
apple_manufacturer_id = config['snipe-it']['manufacturer_id']
logging.info("The configured mosylePro base url is: {} (Pretty sure this needs to be a number too)".format(apple_manufacturer_id))

snipeheaders = {'Authorization': 'Bearer {}'.format(apiKey),'Accept': 'application/json','Content-Type':'application/json'}

# Do some tests to see if the user has updated their settings.conf file
SETTINGS_CORRECT = True
if 'api-mapping' in config:
    logging.error("Looks like you're using the old method for api-mapping. Please use computers-api-mapping and mobile_devices-api-mapping.")
    SETTINGS_CORRECT = False
if not 'user-mapping' in config and (user_args.users or user_args.users_force or user_args.users_inverse):
    logging.error("""You've chosen to check out assets to users in some capacity using a cmdline switch, but not specified how you want to 
    search Snipe IT for the users from mosyle. Make sure you have a 'user-mapping' section in your settings.conf file.""")
    SETTINGS_CORRECT = False

if not SETTINGS_CORRECT:
    raise SystemExit

### Setup Some Functions ###
snipe_api_count = 0
first_snipe_call = None
# This function is run every time a request is made, handles rate limiting for Snipe IT.
def request_handler(r, *args, **kwargs):
    global snipe_api_count
    global first_snipe_call
    if (snipe_base in r.url) and user_args.ratelimited:
        if '"messages":429' in r.text:
            logging.warn("Despite respecting the rate limit of Snipe, we've still been limited. Trying again after sleeping for 2 seconds.")
            time.sleep(2)
            re_req = r.request
            s = requests.Session()
            return s.send(re_req)
        if snipe_api_count == 0:
            first_snipe_call = time.time()
            time.sleep(0.5)
        snipe_api_count += 1
        time_elapsed = (time.time() - first_snipe_call)
        snipe_api_rate = snipe_api_count / time_elapsed
        if snipe_api_rate > 1.95:
            sleep_time = 0.5 + (snipe_api_rate - 1.95)
            logging.debug('Going over snipe rate limit of 120/minute ({}/minute), sleeping for {}'.format(snipe_api_rate,sleep_time))
            time.sleep(sleep_time)
        logging.debug("Made {} requests to Snipe IT in {} seconds, with a request being sent every {} seconds".format(snipe_api_count, time_elapsed, snipe_api_rate))
    if '"messages":429' in r.text:
        logging.error(r.content)
        raise SystemExit("We've been rate limited. Use option -r to respect the built in Snipe IT API rate limit of 120/minute.")
    return r

# Function to lookup a snipe asset by serial number.
def search_snipe_asset(serial):
    api_url = '{}/api/v1/hardware/byserial/{}'.format(snipe_base, serial)
    response = requests.get(api_url, headers=snipeheaders, verify=user_args.do_not_verify_ssl, hooks={'response': request_handler})
    if response.status_code == 200:
        jsonresponse = response.json()
        # Check to make sure there's actually a result
        if 'rows' in jsonresponse:
            if jsonresponse['total'] == 1:
                return jsonresponse['rows'][0]
            else:
                logging.warning('FOUND {} matching assets while searching for: {}'.format(jsonresponse['total'], serial))
                return "MultiMatch"
        elif 'messages' in jsonresponse and jsonresponse['messages'] == 'Asset does not exist.':
            logging.info("No assets match {}".format(serial))
            return "NoMatch"
    else:
        logging.warning('Snipe-IT responded with error code:{} when we tried to look up: {}'.format(response.text, serial))
        logging.debug('{} - {}'.format(response.status_code, response.content))
        return "ERROR"

# Function to get all the asset models
def get_snipe_models():
    api_url = '{}/api/v1/models'.format(snipe_base)
    logging.debug('Calling against: {}'.format(api_url))
    response = requests.get(api_url, headers=snipeheaders, verify=user_args.do_not_verify_ssl, hooks={'response': request_handler})
    if response.status_code == 200:
        jsonresponse = response.json()
        logging.debug("Got a valid response that should have {} models.".format(jsonresponse['total']))
        if jsonresponse['total'] <= len(jsonresponse['rows']) :
            return jsonresponse
        else:
            logging.info("We didn't get enough results so we need to get them again.")
            api_url = '{}/api/v1/models?limit={}'.format(snipe_base, jsonresponse['total'])
            newresponse = requests.get(api_url, headers=snipeheaders, verify=user_args.do_not_verify_ssl, hooks={'response': request_handler})
            if response.status_code == 200:
                newjsonresponse = newresponse.json()
                if newjsonresponse['total'] == len(newjsonresponse['rows']) :
                    return newjsonresponse
                else:
                    logging.error("We couldn't seem to get all of the model numbers")
                    raise SystemExit("Unable to get all model objects from Snipe-IT instanace")
            else:
                logging.error('When we tried to retreive a list of models, Snipe-IT responded with error status code:{} - {}'.format(response.status_code, response.content))
                raise SystemExit("Snipe models API endpoint failed.")
    else:
        logging.error('When we tried to retreive a list of models, Snipe-IT responded with error status code:{} - {}'.format(response.status_code, response.content))
        raise SystemExit("Snipe models API endpoint failed.")

# Function to get all the asset models
def get_snipe_assets(category):
    api_url = '{}/api/v1/hardware'.format(snipe_base)
    count = 0
    all_models = {}
    while True:
        response = requests.get(api_url, headers=snipeheaders,
            verify=user_args.do_not_verify_ssl,
            hooks={'response': request_handler},
            params={'category_id': category, 'limit': 100, 'offset': count})
        if response.status_code == 200:
            logging.debug("Got good page of Snipe assets")
            jsonresponse = response.json()
            for device in jsonresponse['rows']:
                if device['serial'] in all_models:
                    logging.warn(f"{device['serial']} is a duplicate serial number for asset id {device['id']}")
                all_models[device['serial']] = device
            if len(jsonresponse['rows']) + count == jsonresponse['total']:
                break
            count += len(jsonresponse['rows'])
        else:
            logging.error(f"Could not get a list of assets from Snipe: {response.status_code}: {response.text}")
            raise SystemExit()
            break

    return all_models

# Function to search snipe for a user 
def get_snipe_user_id(username):
    if "@" in username:
        username = username.split("@")[0]
    user_id_url = '{}/api/v1/users'.format(snipe_base)
    payload = {
        'search':username,
    }
    logging.debug('The payload for the snipe user search is: {}'.format(payload))
    response = requests.get(user_id_url, headers=snipeheaders, json=payload, hooks={'response': request_handler})
    logging.debug('The response for the snipe user search is: {}'.format(response.json()))
    if response.json().get('rows') != None:
        for user in response.json().get('rows'):
            if "@" in user['username']:
                i = user['username'].split("@")[0]
            else:
                i = user['username']
            if i == username:
                return user['id']
    return "NotFound"

# Function that creates a new Snipe Model - not an asset - with a JSON payload
def create_snipe_model(payload):
    api_url = '{}/api/v1/models'.format(snipe_base)
    logging.debug('Calling to create new snipe model type against: {}\nThe payload for the POST request is:{}\nThe request headers can be found near the start of the output.'.format(api_url, payload))
    response = requests.post(api_url, headers=snipeheaders, json=payload, verify=user_args.do_not_verify_ssl, hooks={'response': request_handler})
    if response.status_code == 200:
        jsonresponse = response.json()
        modelnumbers[jsonresponse['payload']['model_number']] = jsonresponse['payload']['id']
        return True
    else:
        logging.warning('Error code: {} while trying to create a new model.'.format(response.status_code))
        return False

# Function that updates a Snipe Model - not an asset - with a JSON payload
def update_snipe_model(model_id, payload):
    api_url = f"{snipe_base}/api/v1/models/{model_id}"
    logging.debug('Calling to create new snipe model type against: {}\nThe payload for the POST request is:{}\nThe request headers can be found near the start of the output.'.format(api_url, payload))
    response = requests.patch(api_url, headers=snipeheaders, json=payload, verify=user_args.do_not_verify_ssl, hooks={'response': request_handler})
    if response.status_code == 200:
        jsonresponse = response.json()
        modelnumbers[jsonresponse['payload']['model_number']] = jsonresponse['payload']['id']
        return True
    else:
        logging.warning('Error code: {} while trying to update a model.'.format(response.status_code))
        return False

# Function to create a new asset by passing array
def create_snipe_asset(payload):
    api_url = '{}/api/v1/hardware'.format(snipe_base)
    logging.debug('Calling to create a new asset against: {}\nThe payload for the POST request is:{}\nThe request headers can be found near the start of the output.'.format(api_url, payload))
    response = requests.post(api_url, headers=snipeheaders, json=payload, verify=user_args.do_not_verify_ssl, hooks={'response': request_handler})
    logging.debug(response.text)
    if response.status_code == 200:
        logging.debug("Got back status code: 200 - {}".format(response.content))
        return 'AssetCreated', response
    else:
        logging.error('Asset creation failed for asset {} with error {}'.format(payload['name'],response.text))
        return response

# Function that updates a snipe asset with a JSON payload
def update_snipe_asset(snipe_id, payload):
    api_url = '{}/api/v1/hardware/{}'.format(snipe_base, snipe_id, verify=user_args.do_not_verify_ssl)
    logging.debug('The payload for the snipe update is: {}'.format(payload))
    response = requests.patch(api_url, headers=snipeheaders, json=payload, hooks={'response': request_handler})
    # Verify that the payload updated properly.
    goodupdate = True
    if response.status_code == 200:
        logging.debug("Got back status code: 200 - Checking the payload updated properly: If you error here it's because you configure the API mapping right.")
        jsonresponse = response.json()
        for key in payload:
            payload_key = None if payload[key] == "" else payload[key]
            if jsonresponse['payload'][key] != payload_key:
                logging.warning('Unable to update ID: {}. We failed to update the {} field with "{}".'.format(snipe_id, key, payload[key]))
                goodupdate = False
            else:
                logging.info("Sucessfully updated {} with: {}".format(key, payload[key]))
        return goodupdate
    else:
        logging.warning('Whoops. Got an error status code while updating ID {}: {} - {}'.format(snipe_id, response.status_code, response.content))
        return False

# Function that checks in an asset in snipe
def checkin_snipe_asset(asset_id):
    api_url = '{}/api/v1/hardware/{}/checkin'.format(snipe_base, asset_id)
    payload = {
        'note':'checked in by script from mosyle'
    }
    logging.debug('The payload for the snipe checkin is: {}'.format(payload))
    response = requests.post(api_url, headers=snipeheaders, json=payload, hooks={'response': request_handler})
    logging.debug('The response from Snipe IT is: {}'.format(response.json()))
    if response.status_code == 200:
        logging.debug("Got back status code: 200 - {}".format(response.content))
        return "CheckedOut"
    else:
        return response

# Functiono that checks out an asset in snipe
def checkout_snipe_asset(user, asset_id, checked_out_user=None):
    if user == None:
        if checked_out_user != None:
            logging.info("Nobody to checkout the asset to, checking it in")
            checkin_snipe_asset(asset_id)
        return "CheckedOut"
    logging.debug('Asset {} is being checked out to {}'.format(user, asset_id))
    user_id = get_snipe_user_id(user)
    if user_id == 'NotFound':
        logging.info("User {} not found".format(user))
        return "NotFound"
    if checked_out_user == None:
        logging.info("Not checked out, checking out to {}".format(user))
    elif checked_out_user == "NewAsset":
        logging.info("First time this asset will be checked out, checking out to {}".format(user))
    elif checked_out_user['id'] == user_id:
        logging.info(str(asset_id) + " already checked out to user " + user)
        return 'CheckedOut'
    else:
        logging.info("Checking in {} to check it out to {}".format(asset_id,user))
        checkin_snipe_asset(asset_id)
    api_url = '{}/api/v1/hardware/{}/checkout'.format(snipe_base, asset_id)
    logging.info("Checking out {} to check it out to {}".format(asset_id,user))
    payload = {
        'checkout_to_type':'user',
        'assigned_user':user_id,
        'note':'assigned by script from mosyle'
    }
    logging.debug('The payload for the snipe checkin is: {}'.format(payload))
    response = requests.post(api_url, headers=snipeheaders, json=payload, hooks={'response': request_handler})
    logging.debug('The response from Snipe IT is: {}'.format(response.json()))
    if response.status_code == 200:
        logging.debug("Got back status code: 200 - {}".format(response.content))
        return "CheckedOut"
    else:
        logging.error('Asset checkout failed for asset {} with error {}'.format(asset_id,response.text))
        return response

### Run Testing ###
# Report if we're verifying SSL or not. 
logging.info("SSL Verification is set to: {}".format(user_args.do_not_verify_ssl))

# Do some tests to see if the hosts are up.
logging.info("Running tests to see if hosts are up.")
try:
    SNIPE_UP = True if requests.get(snipe_base, verify=user_args.do_not_verify_ssl, hooks={'response': request_handler}).status_code == 200 else False
except:
    SNIPE_UP = False
if SNIPE_UP is False:
    logging.error('Snipe-IT looks like it is down from here. \nPlease check your config in the settings.conf file, or your instance.')
else:
    logging.info('We were able to get a good response from your Snipe-IT instance.')
# Exit if you can't contact SNIPE
if ( SNIPE_UP == False ):
    raise SystemExit("Error: Host could not be contacted.")

logging.info("Finished running our tests.")

### Get Started ###
# Get a list of known models from Snipe
logging.info("Getting a list of computer models that snipe knows about.")
snipemodels = get_snipe_models()
logging.info("Getting a list of Snipe assets to reference from.")
snipe_computer_assets = get_snipe_assets(config['snipe-it']['computer_model_category_id'])
snipe_mobile_assets = get_snipe_assets(config['snipe-it']['mobile_model_category_id'])
all_snipe_assets = snipe_computer_assets | snipe_mobile_assets
logging.info(f"Got a list of {len(snipe_computer_assets)} computers, {len(snipe_mobile_assets)} mobile devices, for a total of {len(all_snipe_assets)} assets from Snipe IT to reference from")
logging.debug("Parsing the {} model results for models with model numbers.".format(len(snipemodels['rows'])))
modelnumbers = {}
modelnames = []
for model in snipemodels['rows']:
    if model['model_number'] == "":
        logging.debug("The model, {}, did not have a model number. Skipping.".format(model['name']))
        continue
    modelnumbers[model['model_number']] =  model['id']
    modelnames.append(model['name'])
logging.info("Our list of models has {} entries.".format(len(modelnumbers)))
logging.debug("Here's the list of the {} models and their id's that we were able to collect:\n{}".format(len(modelnumbers), modelnumbers))

# Get the IDS of all active assets.
mosyle_computer_list = mosyle.get_devices("mac")
mosyle_mobile_list = mosyle.get_devices("ios")
mosyle_types = {
    'computer': mosyle_computer_list,
    'mobile': mosyle_mobile_list
}
TotalNumber = 0
if user_args.computers:
    TotalNumber = len(mosyle_types['computer'])
elif user_args.mobiles:
    TotalNumber = len(mosyle_types['mobile'])
else:
    for mosyle_type in mosyle_types:
        TotalNumber += len(mosyle_types[mosyle_type]) 

# Make sure we have a good list.
if mosyle_computer_list is not None and mosyle_mobile_list is not None:
    logging.info('Received a list of Mosyle assets that had {} entries.'.format(TotalNumber))
else:
    logging.error("We were not able to retreive a list of assets from your Mosyle instance. It's likely that your settings, or credentials are incorrect. Check your settings.conf and verify you can make API calls outside of this system with the credentials found in your settings.conf")
    raise SystemExit("Unable to get Mosyle Computers.")

# After this point we start editing data, so quit if this is a dryrun
if user_args.dryrun:
    raise SystemExit("Dryrun: Complete.")

# From this point on, we're editing data. 
logging.info('Starting to Update Inventory')
CurrentNumber = 0

for mosyle_type in mosyle_types:
    if user_args.computers:
        if mosyle_type != 'computer':
            continue
    if user_args.mobiles:
        if mosyle_type != 'mobile':
            continue
    for md in mosyle_types[mosyle_type]:
        CurrentNumber += 1
        logging.info(f"Processing entry {CurrentNumber} out of {TotalNumber} - Serial: {md['serial_number']} - NAME: {md['device_name']}")
        # Search through the list by ID for all asset information\

        # Check that the model number exists in snipe, if not create it.
        if md['device_model'] not in modelnumbers:
            logging.info(f"Could not find a model ID in snipe for: {md['device_model']}")
            newmodel = {"category_id":config['snipe-it'][f"{mosyle_type}_model_category_id"],"manufacturer_id":apple_manufacturer_id,"name": md['device_model_name'],"model_number":md['device_model']}
            if f"{mosyle_type}_custom_fieldset_id" in config['snipe-it']:
                fieldset_split = config['snipe-it'][f"{mosyle_type}_custom_fieldset_id"]
                newmodel['fieldset_id'] = fieldset_split
            create_snipe_model(newmodel)
        elif md['device_model_name'] not in modelnames:
            update_model = {"name": md['device_model_name']}
            model_id = modelnumbers[md['device_model']]
            logging.info(f"Could not match the model name from Mosyle {md['device_model_name']} in Snipe, it must have changed.")
            update_snipe_model(model_id, update_model)

        # Pass the SN from Mosyle to search for a match in Snipe
        snipe = all_snipe_assets.get(md['serial_number'])

        # Create a new asset if there's no match:
        if snipe is None:
            logging.info("Creating a new asset in snipe for Mosyle ID {} - {}".format(md['serial_number'], md['device_name']))
            # This section checks to see if the asset tag was already put into mosyle, if not it creates one with with mosyle's ID.
            if md['asset_tag'] == '':
                mosyle_asset_tag = md['serial_number']
            else:
                mosyle_asset_tag = md['asset_tag']
            try:
                mosyle_asset_tag = md[config['snipe-it']['asset_tag']]
            except:
                logging.info('No custom configuration found in settings.conf for asset tag name upon asset creation.')
            # Create the payload
            if mosyle_type == 'computer':
                newasset = {'asset_tag': mosyle_asset_tag,'model_id': modelnumbers[md['device_model']], 'name': md['device_name'], 'status_id': defaultStatus,'serial': md['serial_number']}
            elif mosyle_type == 'mobile':
                index = mosyle_asset_tag.find('-')
                mosyle_asset_tag = mosyle_asset_tag[:index] + "-m" + mosyle_asset_tag[index:]
                newasset = {'asset_tag': mosyle_asset_tag, 'model_id': modelnumbers['{}'.format(md['device_model'])], 'name': md['device_name'], 'status_id': defaultStatus,'serial': md['serial_number']}
            if 'serial_number' not in md:
                logging.warning("The serial number is not available in Mosyle. This is normal for DEP enrolled devices that have not yet checked in for the first time. Since there's no serial number yet, we'll skip it for now.")
                continue
            else:
                new_snipe_asset = create_snipe_asset(newasset)
                logging.info(f"Result of creating asset for {md['device_name']} {md['serial_number']}: {new_snipe_asset}")
                if new_snipe_asset[0] != "AssetCreated":
                    continue
                if user_args.users or user_args.users_force or user_args.users_inverse:
                    logging.info('Checking out new item {} to user {}'.format(md['device_name'], md[config['user-mapping']['mosyle_api_field']]))
                    checkout_snipe_asset(md[config['user-mapping']['mosyle_api_field']],new_snipe_asset[1].json()['payload']['id'], "NewAsset")
            snipe = search_snipe_asset(md['serial_number'])

        # Only update if Mosyle has more recent info.
        logging.debug("Snipe asset retreived: " + json.dumps(snipe, indent=4))
        snipe_id = snipe['id']
        snipe_time = snipe['updated_at']['datetime']
        mosyle_time = md['date_last_beat']
        # Check to see that the Mosyle record is newer than the previous Snipe update.
        #if mosyle_time > snipe_time:
        #if True: # uncomment for testing
        payload = {}
        if mosyle_type == 'computer':
            config_mosyle_type = 'computers'
        if mosyle_type == 'mobile':
            config_mosyle_type = 'mobile_devices'
        for snipekey in config['{}-api-mapping'.format(config_mosyle_type)]:
            mosyle_key = config['{}-api-mapping'.format(config_mosyle_type)][snipekey]
            if mosyle_key in md:
                mosyle_value = md[config['{}-api-mapping'.format(config_mosyle_type)][snipekey]]
            else:
                mosyle_value = ''
            
            latestvalue = mosyle_value

            # Need to check that we're not needlessly updating the asset.
            # If it's a custom value it'll fail the first section and send it to except section that will parse custom sections.
            try:
                if snipe[snipekey] != latestvalue:
                    payload[snipekey] = mosyle_value
                else:
                    logging.debug("Skipping the payload, because it already exits.")
            except:
                logging.debug("The snipekey lookup failed, which means it's a custom field. Parsing those to see if it needs to be updated or not.")
                needsupdate = False
                for CustomField in snipe['custom_fields']:
                    if snipe['custom_fields'][CustomField]['field'] == snipekey :
                        if snipe['custom_fields'][CustomField]['value'] != latestvalue:
                            logging.debug("Found the field, and the value needs to be updated from {} to {}".format(snipe['custom_fields'][CustomField]['value'], latestvalue))
                            needsupdate = True
                if needsupdate is True:
                    payload[snipekey] = mosyle_value
                else:
                    logging.debug("Skipping the payload, because it already exists, or the Snipe key we're mapping to doesn't.")
        if len(payload) > 0:
            update_snipe_asset(snipe_id, payload)

        if ((user_args.users or user_args.users_inverse) and (snipe['assigned_to'] == None) == user_args.users) or user_args.users_force:
            if snipe['status_label']['status_meta'] in ('deployable', 'deployed'):
                checkout_snipe_asset(md.get(config['user-mapping']['mosyle_api_field']), snipe_id, snipe['assigned_to'])
            else:
                logging.info("Can't checkout {} since the status isn't set to deployable".format(md['device_name']))
        '''
        # Update/Sync the Snipe Asset Tag Number back to mosyle
        if md['asset_tag'] != snipe['asset_tag']:
            logging.info("mosyle doesn't have the same asset tag as SNIPE so we'll update it because it should be authoritative.")
            mosyle.update_devices(md['serial_number'], {"asset_tag": snipe['asset_tag']})
        '''