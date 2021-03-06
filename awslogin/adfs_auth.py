#
# This file was adapted from https://github.com/venth/aws-adfs. Thanks to https://github.com/venth for his work on
# figuring this out
#

import requests
from bs4 import BeautifulSoup
import re
import lxml.etree as ET
from urllib.parse import urlparse, parse_qs


# idpentryurl: The initial url that starts the authentication process.
idpentryurl = 'https://awslogin.byu.edu:443/adfs/ls/IdpInitiatedSignOn.aspx?loginToRp=urn:amazon:webservices'

def authenticate(username, password):
    # Initiate session handler
    session = requests.Session()

    # Programmatically get the SAML assertion
    # Opens the initial IdP url and follows all of the HTTP302 redirects, and
    # gets the resulting login page
    formresponse = session.get(idpentryurl, verify=True)

    # Capture the idpauthformsubmiturl, which is the final url after all the 302s
    idpauthformsubmiturl = formresponse.url

    # Parse the response and extract all the necessary values
    # in order to build a dictionary of all of the form values the IdP expects
    formsoup = BeautifulSoup(formresponse.text, "lxml")
    payload = {}

    for inputtag in formsoup.find_all(re.compile('(INPUT|input)')):
        name = inputtag.get('name', '')
        value = inputtag.get('value', '')
        if "user" in name.lower():
            # Make an educated guess that this is the right field for the username
            payload[name] = username
        elif "email" in name.lower():
            # Some IdPs also label the username field as 'email'
            payload[name] = username
        elif "pass" in name.lower():
            # Make an educated guess that this is the right field for the password
            payload[name] = password
        else:
            # Simply populate the parameter with the existing value (picks up hidden fields in the login form)
            payload[name] = value

    # Debug the parameter payload if needed
    # Use with caution since this will print sensitive output to the screen
    # print payload

    # Some IdPs don't explicitly set a form action, but if one is set we should
    # build the idpauthformsubmiturl by combining the scheme and hostname
    # from the entry url with the form action target
    # If the action tag doesn't exist, we just stick with the
    # idpauthformsubmiturl above
    for inputtag in formsoup.find_all(re.compile('(FORM|form)')):
        action = inputtag.get('action')
        loginid = inputtag.get('id')
        if (action and loginid == "loginForm"):
            parsedurl = urlparse(idpentryurl)
            idpauthformsubmiturl = parsedurl.scheme + "://" + parsedurl.netloc + action

    # Performs the submission of the IdP login form with the above post data
    response = session.post(
        idpauthformsubmiturl, data=payload, verify=True)

    html_response = ET.fromstring(response.text, ET.HTMLParser())

    # Perform DUO MFA
    auth_signature, duo_request_signature = _authenticate_duo(html_response, True, session)

    return html_response, session, auth_signature, duo_request_signature

_headers = {
    'Accept-Language': 'en',
    'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko',
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'Accept': 'text/plain, */*; q=0.01',
}

def _authenticate_duo(html_response, roles_page_url, session):
    duo_host = _duo_host(html_response)
    duo_request_signature = _duo_request_signature(html_response)

    print("Sending request for authentication")
    (sid, preferred_factor, preferred_device), initiated = _initiate_authentication(
        duo_host,
        duo_request_signature,
        roles_page_url,
        session
    )
    if initiated:
        transaction_id = _begin_authentication_transaction(
            duo_host,
            sid,
            preferred_factor,
            preferred_device,
            session
        )

        print("Waiting for additional authentication")
        _verify_that_code_was_sent(
            duo_host,
            sid,
            transaction_id,
            session
        )
        auth_signature = _authentication_result(
            duo_host,
            sid,
            transaction_id,
            session,
        )

        return auth_signature, duo_request_signature
    else:
        raise RuntimeError("DUO Transaction Not Initiated")


def _authentication_result(
        duo_host,
        sid,
        duo_transaction_id,
        session
):
    status_for_url = "https://{}/frame/status".format(duo_host)
    response = session.post(
        status_for_url,
        verify=True,
        headers=_headers,
        data={
            'sid': sid,
            'txid': duo_transaction_id
        }
    )

    if response.status_code != 200:
        raise RuntimeError(
            u'Issues during retrieval of a code entered into '
            u'the device. The error response {}'.format(
                response
            )
        )

    json_response = response.json()
    if json_response['stat'] != 'OK':
        raise RuntimeError(
            u'There was an issue during retrieval of a code entered into the device.'
            u' The error response: {}'.format(
                response.text
            )
        )

    if json_response['response']['status_code'] != 'allow':
        raise RuntimeError(
            u'There was an issue during retrieval of a code entered into the device.'
            u' The error response: {}'.format(
                response.text
            )
        )

    auth_signature = response.json()['response']['cookie']
    return auth_signature


def _verify_that_code_was_sent(duo_host, sid, duo_transaction_id, session):
    status_for_url = "https://{}/frame/status".format(duo_host)
    response = session.post(
        status_for_url,
        verify=True,
        headers=_headers,
        data={
            'sid': sid,
            'txid': duo_transaction_id
        }
    )

    if response.status_code != 200:
        raise RuntimeError(
            u'Issues during sending code to the devide. The error response {}'.format(
                response
            )
        )

    json_response = response.json()
    if json_response['stat'] != 'OK':
        raise RuntimeError(
            u'There was an issue during sending code to the device. The error response: {}'.format(
                response.text
            )
        )

    if json_response['response']['status_code'] != 'pushed':
        raise RuntimeError(
            u'There was an issue during sending code to the device. The error response: {}'.format(
                response.text
            )
        )


_tx_pattern = re.compile("(TX\|[^:]+):APP.+")


def _tx(request_signature):
    m = _tx_pattern.search(request_signature)
    return m.group(1)


def _initiate_authentication(duo_host, duo_request_signature, roles_page_url, session):
    prompt_for_url = 'https://{}/frame/web/v1/auth'.format(duo_host)
    parent = "{}{}".format(
        roles_page_url,
        "&java_version="
        "&flash_version="
        "&screen_resolution_width=1280"
        "&screen_resolution_height=800"
        "&color_depth=24"
    )
    response = session.post(
        prompt_for_url,
        verify=True,
        headers={
            'Host': duo_host,
            'User-Agent': "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.12; rv:52.0) Gecko/20100101 Firefox/52.0",
            'Accept': "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            'Accept-Language': "en-US,en;q=0.5",
            'Accept-Encoding': "gzip, deflate, br",
            'DNT': "1",
            'Upgrade-Insecure-Requests': "1",
            'Content-Type': "application/x-www-form-urlencoded",
        },
        allow_redirects=True,
        params={
            'tx': _tx(duo_request_signature),
            'parent': parent,
            'v': '2.3',
        },
        data={
            'parent': parent,
            'java_version': '',
            'flash_version': '22.0.0.209',
            'screen_resolution_width': '1280',
            'screen_resolution_height': '800',
            'color_depth': '24',
        }
    )

    if response.status_code != 200 or response.url is None:
        return None, False

    o = urlparse(response.url)
    query = parse_qs(o.query)

    if 'sid' not in query:
        return None, False

    sid = query['sid']
    html_response = ET.fromstring(response.text, ET.HTMLParser())
    preferred_factor = _preferred_factor(html_response)
    preferred_device = _preferred_device(html_response)
    return (sid, preferred_factor, preferred_device), True


def _preferred_factor(html_response):
    preferred_factor_query = './/input[@name="preferred_factor"]'
    element = html_response.find(preferred_factor_query)
    return element.get('value')


def _preferred_device(html_response):
    preferred_device_query = './/input[@name="preferred_device"]'
    element = html_response.find(preferred_device_query)
    return element.get('value')


def _begin_authentication_transaction(duo_host, sid, preferred_factor, preferred_device, session):
    prompt_for_url = "https://{}/frame/prompt".format(duo_host)
    response = session.post(
        prompt_for_url,
        verify=True,
        headers=_headers,
        data={
            'sid': sid,
            'factor': preferred_factor,
            'device': preferred_device,
            'out_of_date': ''
        }
    )

    if response.status_code != 200:
        raise RuntimeError(
            u'Issues during beginning of the authentication process. The error response {}'.format(
                response
            )
        )

    json_response = response.json()
    if json_response['stat'] != 'OK':
        raise RuntimeError(
            u'Cannot begin authentication process. The error response: {}'.format(response.text)
        )

    return json_response['response']['txid']


_duo_host_pattern = re.compile("'host': '([^']+)'")


def _duo_host(html_response):
    duo_host_query = './/form[@id="duo_form"]/following-sibling::script'
    element = html_response.xpath(duo_host_query)[0]
    m = _duo_host_pattern.search(element.text)
    return m.group(1)


_duo_signature_pattern = re.compile("'sig_request': '([^']+)'")


def _duo_request_signature(html_response):
    duo_signature_query = './/form[@id="duo_form"]/following-sibling::script'
    element = html_response.xpath(duo_signature_query)[0]
    m = _duo_signature_pattern.search(element.text)
    return m.group(1)
