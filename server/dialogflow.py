import json
import logging
import tornado.gen

from tornado_http_auth import BasicAuthMixin
from pywakeonlan.wakeonlan import send_magic_packet
from server.base import BaseHandler

class DialogFlowHandler(BasicAuthMixin, BaseHandler):
    lastComputer = {}

    def initialize(self, credentials):
        self.credentials = credentials

    def check_xsrf_cookie(self):
        """
        Disable check since DialogFlow logs in via basic HTTP authentication
        """
        return True

    def prepare(self):
        self.get_authenticated_user(check_credentials_func=self.credentials.get, realm='Protected')

    def get(self):
        self.write("This is meant to be a webhook for DialogFlow")

    @tornado.gen.coroutine
    def get_wol_mac(self, userid, computer):
        laptop_mac, desktop_mac = yield self.get_macs(userid)

        if computer.strip().lower() == "laptop":
            return laptop_mac
        else:
            return desktop_mac

    @tornado.gen.coroutine
    def post(self):
        data = json.loads(self.request.body.decode('utf-8'))

        # Skip if already answered, e.g. saying "Hi!" will be fulfilled by "Small Talk"
        if 'fulfillmentText' in data['queryResult']:
            self.write(json.dumps({}))
            self.set_header("Content-type", "application/json")
            return

        # Make sure the user is logged in and provided a valid access token for a signed-up user
        if 'originalDetectIntentRequest' not in data or \
           'payload' not in data['originalDetectIntentRequest'] or \
           'user' not in data['originalDetectIntentRequest']['payload'] or \
           'accessToken' not in data['originalDetectIntentRequest']['payload']['user']:
            self.write(json.dumps({ "fulfillmentText": "You must be logged in." }))
            self.set_header("Content-type", "application/json")
            return

        userid = yield self.getUserIDFromToken(data['originalDetectIntentRequest']['payload']['user']['accessToken'])

        if not userid:
            logging.error("Invalid access token - userid: "+str(userid)+", data:"+str(data))
            self.write(json.dumps({ "fulfillmentText": "Invalid access token." }))
            self.set_header("Content-type", "application/json")
            return

        response = "Sorry, I'm not sure how to answer that."
        longResponse = None

        # Determine command/query and respond appropriately
        try:
            intent = data['queryResult']['intent']['displayName']
            params = data['queryResult']['parameters']

            if intent == "Computer Command":
                command = params['Command']
                computer = params['Computer']
                x = params['X']
                url = params['url']

                # Update last computer used
                if computer:
                    self.lastComputer[userid] = computer
                # If no computer specified, use last, if available
                elif userid in self.lastComputer:
                    computer = self.lastComputer[userid]

                # Only command we handle is the WOL packet
                if command == "power on":
                    if computer:
                        mac = yield self.get_wol_mac(userid, computer)

                        if mac:
                            send_magic_packet(mac, port=9)
                            response = "Woke your "+computer
                        else:
                            response = "Your "+computer+" is not set up for wake-on-LAN"
                    else:
                        response = "Please specify which computer you are asking about"
                else:
                    if userid in self.clients and computer in self.clients[userid]:
                        self.clients[userid][computer].write_message(json.dumps({
                            "command": { "command": command, "x": x, "url": url }
                        }))
                        response, longResponse = yield self.clients[userid][computer].wait_response()

                        if not response:
                            response = "Command sent to "+computer
                    elif computer:
                        response = "Your "+computer+" is not currently online"
                    else:
                        response = "Please specify which computer you are asking about"

                    # TODO
                    # If this takes too long, then immediately respond "Command sent to laptop"
                    # and then do this: https://productforums.google.com/forum/#!topic/dialogflow/HeXqMLQs6ok;context-place=forum/dialogflow
                    # saving context and later returning response or something
            elif intent == "Computer Query":
                value = params['Value']
                x = params['X']
                computer = params['Computer']

                # Update last computer used
                if computer:
                    self.lastComputer[userid] = computer
                # If no computer specified, use last, if available
                elif userid in self.lastComputer:
                    computer = self.lastComputer[userid]

                # Only query we handle is the "where is my laptop/desktop"
                if value == "where":
                    if computer:
                        if userid in self.clients and computer in self.clients[userid]:
                            ip = self.clients[userid][computer].ip
                            response = "Unknown location for your "+computer

                            if ip:
                                if ip == self.serverIp:
                                    response = "Your "+computer+" is at home"
                                else:
                                    data = self.gi.record_by_addr(ip)

                                    if data and "city" in data and "region_name" in data and "country_name" in data:
                                        city = data["city"]
                                        region = data["region_name"]
                                        country = data["country_name"]
                                        response = "Your "+computer+" is in "+city+", "+region+", "+country+" ("+ip+")"
                        else:
                            response = "Could not find location of your "+computer
                    else:
                        response = "Please specify which computer you are asking about"
                else:
                    if userid in self.clients and computer in self.clients[userid]:
                        self.clients[userid][computer].write_message(json.dumps({
                            "query": { "value": value, "x": x }
                        }))
                        response, longResponse = yield self.clients[userid][computer].wait_response()

                        if not response:
                            response = "Your "+computer+" did not respond"
                    elif computer:
                        response = "Your "+computer+" is not currently online"
                    else:
                        response = "Please specify which computer you are asking about"
        except KeyError:
            pass

        #"source": string,
        #"payload": { },
        #"outputContexts": [ { object(Context) } ],
        #"followupEventInput": { object(EventInput) },
        #"fulfillmentMessages": [ { response } ],

        # If desired, display one thing and say another. This is useful for
        # example when the displayed text is a file name and you only want to
        # read the most relevant part of it.
        if longResponse:
            json_response = json.dumps({
                "fulfillmentMessages": [{
                    "platform": "ACTIONS_ON_GOOGLE",
                    "simpleResponses": {
                        "simpleResponses": [{
                            "textToSpeech": response,
                            "displayText": longResponse
                        }]
                    }
                }]})
        else:
            json_response = json.dumps({ "fulfillmentText": response })

        self.write(json_response)
        self.set_header("Content-type", "application/json")

