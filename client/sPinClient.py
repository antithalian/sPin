#!/usr/bin/env python3
# John Sullivan (jsulli28), Jozef Porubcin (jporubci)
# sPin


# http.client: to get catalog
# json: to parse catalog
# random: to randomize the order of entries in the catalog
# time: to check if a peer has timed out
# socket: to connect to a peer
# uuid: to uniquely identify copies of a file across time
# os: to get size of file for sending file as a message using TCP

import http.client
import json
import random
import time
import socket
import uuid
import math
import os


# CATALOG_SERVER: address and port of name server
# ENTRY_TYPE: personal identifier for distinguishing which entries in the catalog are sPin servers
# TIMEOUT: maximum seconds since any given sPin peer last communicated with the name server before the sPin peer is considered dead

CATALOG_SERVER = 'catalog.cse.nd.edu:9097'
ENTRY_TYPE = 'sPin'
TIMEOUT = 60
K_DENOM = 3 # denominator for determining k


class sPinPeer:
    def __init__(self, address, port, lastheardfrom):
        self.address = address
        self.port = port
        self.lastheardfrom = lastheardfrom

class sPinClient:
    def __init__(self):
        # TCP socket
        self.s = None
        self.addr = None
        self.port = None
        
        
    # Connects self's socket to a live sPin peer
    def _lookup_peer(self):
        
        # Get catalog
        http_conn = http.client.HTTPConnection(CATALOG_SERVER)
        http_conn.request('GET', '/query.json')
        
        # Parse catalog
        catalog = json.loads(http_conn.getresponse().read())
        http_conn.close()
        
        # Shuffle catalog
        random.shuffle(catalog)
        
        # Iterate through catalog
        for entry in catalog:
            
            # If the entry dict has the necessary keys
            if all(key in entry for key in ('type', 'address', 'port', 'lastheardfrom')):
                
                # If the entry is a live sPin peer
                if entry['type'] == ENTRY_TYPE and entry['lastheardfrom'] >= time.time_ns() / 1000000000.0 - TIMEOUT:
                    
                    # Try to connect to the peer
                    try:
                        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        self.s.connect((entry['address'], entry['port']))
                        self.addr = entry['address']
                        self.port = entry['port']
                        return
                    except:
                        self.s.close()
        
        print('Failed to connect to a live peer')
        
        
    # Adds a file to the network
    def sPinADD(self, filepath):
        
        self._lookup_peer()
        if self.s == None:
            return
        
        peers = self.get_peers()
        
        # figure out k
        k = math.ceil(len(peers) / K_DENOM)

        # figure out which peers to pin to
        pin_to = random.choices(peers, k)
        
        try:
            f = open(filepath, 'rb')
        except:
            print('Failed to open file: ' + filepath)
            return
        
        # HTTP POST to all peers
        async for peer in pin_to:
            pass

        #
        http_conn = http.client.HTTPConnection(self.addr + ':' + str(self.port))
        http_conn.request('POST', '/' + str(uuid.uuid4()), f)
        f.close()
        http_conn.close()
        
        
    # Gets the file associated with the given key
    def sPinGET(self, key):
        
        self._lookup_peer()
        if self.s == None:
            return
        
        # HTTP GET to peer
        pass
        
        
    # Requests deletion of the file associated with the given key
    def sPinDEL(self, key):
        
        self._lookup_peer()
        if self.s == None:
            return
        
        # HTTP POST to peer
        pass
