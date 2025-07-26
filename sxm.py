import argparse
import requests
import base64
import urllib.parse
import json
import time, datetime
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
import configparser
config = configparser.ConfigParser()
import random
import threading

class SiriusXM:
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
    REST_FORMAT = 'https://api.edge-gateway.siriusxm.com/{}'
    CDN_URL = "https://imgsrv-sxm-prod-device.streaming.siriusxm.com/{}"

    def __init__(self, username, password):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': self.USER_AGENT})
        self.username = username
        self.password = password
        self.playlists = {}
        self.channels = None
        self.channel_ref = None
        self.m3u8dat = None
        self.stream_urls = {}
        self.xtra_streams = {}
        self.prevcount = 0
        threading.Thread(target=self.cleanup_streaminfo, daemon=True).start()
    
    @staticmethod
    def log(x):
        print('{} <SiriusXM>: {}'.format(datetime.datetime.now().strftime('%d.%b %Y %H:%M:%S'), x))


    #TODO: Figure out if authentication is a valid method anymore. It might need a new login each time.
    def is_logged_in(self):
        return 'Authorization' in self.session.headers

    def is_session_authenticated(self):
        return 'Authorization' in self.session.headers
    
    def sfetch(self, url,retries=0): # if this errors again, its likely VLC or something holding old urls prob, gotta force a refresh then somehow
        if retries >= 2:
            self.log("Failed to reauthenticate after 401. Dump: ",self.session.headers)
        res = self.session.get(url)
        if res.status_code != 200:
            if res.status_code >= 400 and res.status_code < 500:
                self.login()
                self.authenticate()
                return self.sfetch(self,url,retries=retries+1)
            self.log("Failed to recieve stream data. Error code {}".format(str(res.status_code)))
            return None
                
        return res.content

    def get(self, method, params={}, authenticate=True, retries=0):
        if retries >= 3:
            self.log("Max retries hit on {} using method Get".format(method))
            return None
        if authenticate and not self.is_session_authenticated() and not self.authenticate():
            self.log('Unable to authenticate')
            return None

        res = self.session.get(self.REST_FORMAT.format(method), params=params)
        if res.status_code != 200:
            if res.status_code >= 400 and res.status_code < 500:
                self.login()
                self.authenticate()
                return self.post(method, postdata=params, authenticate=authenticate, retries=retries+1)
            self.log('Received status code {} for method \'{}\''.format(res.status_code, method))
            return None

        try:
            return res.json()
        except ValueError:
            self.log('Error decoding json for method \'{}\''.format(method))
            return None

    def post(self, method, postdata, authenticate=True, headers={},retries=0):
        if retries >= 3:
            self.log("Max retries hit on {} using method Post".format(method))
            return None
        if authenticate and not self.is_session_authenticated() and not self.authenticate():
            self.log('Unable to authenticate')
            return None

        res = self.session.post(self.REST_FORMAT.format(method), data=json.dumps(postdata),headers=headers)
        if res.status_code != 200 and res.status_code != 201:
            if res.status_code >= 400 and res.status_code < 500:
                self.login()
                self.authenticate()
                return self.post(method,postdata,authenticate,headers,retries+1)
            self.log('Received status code {} for method \'{}\''.format(res.status_code, method))
            return None

        resjson = res.json()
        bearer_token = resjson["grant"] if "grant" in resjson else resjson["accessToken"] if "accessToken" in resjson else None
        if bearer_token != None:
            self.session.headers.update({"Authorization": f"Bearer {bearer_token}"})

        try:
            return resjson
        except ValueError:
            self.log('Error decoding json for method \'{}\''.format(method))
            return None

    def login(self):
        # Four layer process
        # Assuming the login can work separate from Auth, this is split into two connections:
        # 1) device acknowledge
        # 2) grant anonymous permission
        # The following is reserved for Authentication:
        # Login
        # Affirm Authentication

        # do a completely new session
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': self.USER_AGENT})

        postdata = {
            'devicePlatform': "web-desktop",
            'deviceAttributes': {
                'browser': {
                    'browserVersion': "7.74.0",
                    'userAgent': self.USER_AGENT,
                    'sdk': 'web',
                    'app': 'web',
                    'sdkVersion': "7.74.0",
                    'appVersion': "7.74.0"
                }
            },
            'grantVersion': 'v2'
        }
        sxmheaders = {
            "x-sxm-tenant":"sxm" # required, but not used everywhere
        }
        data = self.post('device/v1/devices', postdata, authenticate=False,headers=sxmheaders)
        if not data:
            self.log("Error creating device session:",data)
            return False

        # Once device is registered, grant anonymous permissions 
        data = self.post('session/v1/sessions/anonymous', {}, authenticate=False,headers=sxmheaders)
        if not data:
            self.log("Error validating anonymous session:",data)
            return False
        try:
            return "accessToken" in data and self.is_logged_in()
        except KeyError:
            self.log('Error decoding json response for login')
            return False
        


    def authenticate(self):
        if not self.is_logged_in() and not self.login():
            self.log('Unable to authenticate because login failed')
            return False

        postdata = {
            "handle": self.username,
            "password": self.password
        }
        data = self.post('identity/v1/identities/authenticate/password', postdata, authenticate=False)
        if not data:
            return False

        
        autheddata = self.post('session/v1/sessions/authenticated', {}, authenticate=False)

        try:
            return autheddata['sessionType'] == "authenticated" and self.is_session_authenticated()
        except KeyError:
            self.log('Error parsing json response for authentication')
            return False

    def get_playlist(self):
        # Not 100% sure how this was working previously, but modern times
        # mostly fetch info via json, so we have to make the m3u8 from scratch
        # Create our own M3U8 from scratch, include all we found
        if not self.channels:
            self.get_channels()
        if not self.m3u8dat:
            data = []
            data.append("#EXTM3U")
            m3umetadata = """#EXTINF:-1 tvg-id="{}" tvg-logo="{}" group-title="{}",{}\n{}"""
            for channel in self.channels:
                #TODO: Work on finding the proper M3U8 metadata needed.
                title = channel["title"]
                genre = channel["genre"]
                logo = channel["logo"]
                channel_id = channel["channel_id"]
                url = "/listen/{}".format(channel["id"])
                formattedm3udata = m3umetadata.format(channel_id,logo,genre,title,url)
                data.append(formattedm3udata)
            self.m3u8dat = "\n".join(data)
        
        return self.m3u8dat

    def get_channels(self):
        # download channel list if necessary
        # todo: find out if the container ID or the UUID changes; how to auto fetch if so.
        # channel list is split up. gotta get every channel

        if not self.channels:
            self.channels = []
            # todo: this is how the web traffic processed the channels, might not be needed though
            initData = {
                "containerConfiguration": {
                    "3JoBfOCIwo6FmTpzM1S2H7": {
                        "filter": {
                            "one": {
                                "filterId": "all"
                            }
                        },
                        "sets": {
                            "5mqCLZ21qAwnufKT8puUiM": {
                                "sort": {
                                    "sortId": "CHANNEL_NUMBER_ASC"
                                }
                            }
                        }
                    }
                },
                "pagination": {
                    "offset": {
                        "containerLimit": 3,
                        "setItemsLimit": 50
                    }
                },
                "deviceCapabilities": {
                    "supportsDownloads": False
                }
            }
            data = self.post('browse/v1/pages/curated-grouping/403ab6a5-d3c9-4c2a-a722-a94a6a5fd056/view', initData)
            if not data:
                self.log('Unable to get init channel list')
                return (None, None)
            for channel in data["page"]["containers"][0]["sets"][0]["items"]:
                title = channel["entity"]["texts"]["title"]["default"]
                description = channel["entity"]["texts"]["description"]["default"]
                genre = channel["decorations"]["genre"] if "genre" in channel["decorations"] else ""
                channel_id = channel["decorations"]["channelNumber"]
                channel_type = channel["actions"]["play"][0]["entity"]["type"]
                logo = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["url"]
                logo_width = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["width"]
                logo_height = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["height"]
                id = channel["entity"]["id"]
                jsonlogo = json.dumps({
                    "key": logo,
                    "edits":[
                        {"format":{"type":"jpeg"}},
                        {"resize":{"width":logo_width,"height":logo_height}}
                    ]
                },separators=(',', ':'))
                b64logo = base64.b64encode(jsonlogo.encode("ascii")).decode("utf-8")
                self.channels.append({
                    "title": title,
                    "description": description,
                    "genre": genre,
                    "channel_id": channel_id,
                    "channel_type": channel_type,
                    "logo":  self.CDN_URL.format(b64logo),
                    "url": "/listen/{}".format(id),
                    "id": id
                })
                
            channellen = data["page"]["containers"][0]["sets"][0]["pagination"]["offset"]["size"]
            for offset in range(50,channellen,50):
                postdata = {
                    "filter": {
                        "one": {
                        "filterId": "all"
                        }
                    },
                    "sets": {
                        "5mqCLZ21qAwnufKT8puUiM": {
                        "sort": {
                            "sortId": "CHANNEL_NUMBER_ASC"
                        },
                        "pagination": {
                            "offset": {
                            "setItemsOffset": offset,
                            "setItemsLimit": 50
                            }
                        }
                        }
                    },
                    "pagination": {
                        "offset": {
                        "setItemsLimit": 50
                        }
                    }
                }
                data = self.post('browse/v1/pages/curated-grouping/403ab6a5-d3c9-4c2a-a722-a94a6a5fd056/containers/3JoBfOCIwo6FmTpzM1S2H7/view', postdata, initData)
                if not data:
                    self.log('Unable to get fetch channel list chunk')
                    return (None, None)
                for channel in data["container"]["sets"][0]["items"]:
                    title = channel["entity"]["texts"]["title"]["default"]
                    description = channel["entity"]["texts"]["description"]["default"]
                    genre = channel["decorations"]["genre"] if "genre" in channel["decorations"] else ""
                    channel_id = channel["decorations"]["channelNumber"]
                    channel_type = channel["actions"]["play"][0]["entity"]["type"]
                    logo = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["url"]
                    logo_width = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["width"]
                    logo_height = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["height"]
                    id = channel["entity"]["id"]
                    jsonlogo = json.dumps({
                        "key": logo,
                        "edits":[
                            {"format":{"type":"jpeg"}},
                            {"resize":{"width":logo_width,"height":logo_height}}
                        ]
                    },separators=(',', ':'))
                    b64logo = base64.b64encode(jsonlogo.encode("ascii")).decode("utf-8")
                    self.channels.append({
                        "title": title,
                        "description": description,
                        "genre": genre,
                        "channel_id": channel_id,
                        "channel_type": channel_type,
                        "logo":  self.CDN_URL.format(b64logo),
                        "url": "/listen/{}".format(id),
                        "id": id
                    })

        return self.channels

    #temporary patch, should do a reverse index lookup table
    def get_channel_info(self,id):
        if not self.channels:
            self.get_channels()
        for ch in self.channels:
            if id == ch["id"]:
                return ch
        return None

    def get_tuner(self,id):
        channel_info = self.get_channel_info(id)
        channel_type = channel_info["channel_type"] if channel_info and "channel_type" in channel_info else "channel-linear"
        isXtra = channel_type == "channel-xtra"
        if id in self.stream_urls and isXtra == False:
            return self.stream_urls[id]
        #if contextId != None and contextId in self.xtra_streams and isXtra == True:
        #    return self.xtra_streams[contextId]
        postdata = {
            "id":id,
            "type":channel_type,
            "hlsVersion":"V3",
            "mtcVersion":"V2"
        }

        hasContextId = False
        contextId = ''
        if isXtra and id in self.stream_urls and "sourceContextId" in self.stream_urls[id]:
            hasContextId = True
            contextId = self.stream_urls[id]["sourceContextId"]
        if hasContextId:
            postdata["sourceContextId"] = contextId
        else:
            postdata["manifestVariant"] = "WEB" if channel_type == "channel-linear" else "FULL"
        
        tunerUrl = 'playback/play/v1/tuneSource' if not hasContextId else 'playback/play/v1/peek'
        data = self.post(tunerUrl,postdata,authenticate=True)
        if not data:
            self.log("Couldn't tune channel.")
            return False
        #TODO: add secondary cause why not
        streaminfo = {}
        primarystreamurl = data["streams"][0]["urls"][0]["url"]
        sessionId = None
        sourceContextId = None
        if isXtra:
            sessionId = str(random.randint((10**37),(10**38)))
            sourceContextId = data["streams"][0]["metadata"]["xtra"]["sourceContextId"] 
            streaminfo["sessionId"] = sessionId
            streaminfo["expires"] = time.time()+600 # expire/remove this stream in 10 minutes
        base_url, m3u8_loc = primarystreamurl.rsplit('/', 1)
        streaminfo["base_url"] = base_url
        streaminfo["sources"] = m3u8_loc
        streaminfo["chid"] = base_url.split('/')[-2]
        streaminfo["sourceContextId"] = sourceContextId
        streamdata = self.sfetch(primarystreamurl)
        if not streamdata:
            self.log("Failed to fetch m3u8 stream details")
            return False
        streamdata = streamdata.decode("utf-8")
        # TODO: make this have options for other qualities (url parameter?)
        for line in streamdata.splitlines():
            if line.find("256k") > 0 and line.endswith("m3u8"):
                streaminfo["quality"] = line
                streaminfo["HLS"] = line.split("/")[0]
        if isXtra:
            self.xtra_streams[ sessionId ] = streaminfo
        self.stream_urls[id] = streaminfo
        return streaminfo
    
    def get_tuner_cached(self,id,sessionId):
            return self.xtra_streams[sessionId]
    
    def cleanup_streaminfo(self,delay=600):
        while True:
            now = time.time()
            keys_to_delete = [sessionId for sessionId in self.xtra_streams.keys() if self.xtra_streams[sessionId]["expires"] < now]
            for k in keys_to_delete:
                del self.xtra_streams[k]
            time.sleep(delay)
    
    def get_channel(self, id):
        # Hit a wall in how I wanted to implement this, but this is what I ended up doing:
        # Caching the /tuneSource url provided, and associating it to the /listen UUID
        # this prevents multiple hits to /tuneSource and more to the Streaming CDN
        # potentially speeding this part of the process up, as well as being more subtle
        # in main site web traffic.
        streaminfo = self.get_tuner(id)
        sessionId = streaminfo["sessionId"] if "sessionId" in streaminfo and streaminfo["sessionId"] != None else ''
        aacurl = "{}/{}".format(streaminfo["base_url"],streaminfo["quality"])
        # fetch the list of aac files
        data = self.sfetch(aacurl)
        if not data:
            self.log("failed to fetch AAC stream list")
            return False
        data = data.decode("utf-8")
        data = data.replace("https://api.edge-gateway.siriusxm.com/playback/key/v1/","/key/",1)
        lineoutput = []
        lines = data.splitlines()
        for x in range(len(lines)):
            if lines[x].rstrip().endswith('.aac'):
                lines[x] = '{}/{}?{}'.format(id, lines[x],sessionId)
        return '\n'.join(lines).encode('utf-8')

    def get_segment(self,id,seg,sessionId=''):
        streaminfo = None
        if sessionId != '':
            streaminfo = self.get_tuner_cached(id,sessionId)      
        else:      
            streaminfo = self.get_tuner(id)
        baseurl = streaminfo["base_url"]
        HLStag = streaminfo["HLS"]
        segmenturl = "{}/{}/{}".format(baseurl,HLStag,seg)
        data = self.sfetch(segmenturl)
        return data
        
    def getAESkey(self,uuid):
        data = self.get("playback/key/v1/{}".format(uuid))
        if not data:
            self.log("AES Key fetch error.")
            return False
        return data["key"]
    

def make_sirius_handler(sxm):
    class SiriusHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.find('.m3u8') > 0:
                data = sxm.get_playlist()
                if data:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/x-mpegURL')
                    self.end_headers()
                    self.wfile.write(bytes(data, 'utf-8'))
                    return
                else:
                    self.send_response(500)
                    self.end_headers()
            elif self.path.find('.aac') > 0:
                dirsplit = self.path.split("/")
                id = dirsplit[-2]
                seg = dirsplit[-1]
                data = None
                if self.path.find('?') > 0:
                    contextId = self.path.split("?")[-1]
                    data = sxm.get_segment(id,seg,contextId)
                else:
                    data = sxm.get_segment(id,seg)
                
                if data:
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/x-aac')
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(500)
                    self.end_headers()
            elif self.path.startswith('/key/'):
                split = self.path.split("/")
                uuid = split[-1]
                key = base64.b64decode(sxm.getAESkey(uuid))
                if not key:
                    self.send_response(500)
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(key)
            elif self.path.startswith("/listen/"):
                data = sxm.get_channel(self.path.split('/')[-1])
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-mpegURL')
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(500)
                self.end_headers()
    return SiriusHandler



if __name__ == '__main__':
    config.read('config.ini')
    email = config.get("account","email")
    password = config.get("account","password")

    ip = config.get("settings","ip")
    port = int(config.get("settings","port"))
    print("Starting server at {}:{}".format(ip, port))
    sxm = SiriusXM(email, password)
    httpd = HTTPServer((ip, port), make_sirius_handler(sxm))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
