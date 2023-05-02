#!/usr/bin/env python3

# John Sullivan (jsulli28), Jozef Porubcin (jporubci)
# sPinServer.py

import asyncio, aiohttp
from aiohttp import web
import socket # need constants
import uuid, json, os, time, collections, shutil, random, math

import sys
import pprint # for debug

import ckpt
import pin_funcs

class sPinServer:

    # get DEBUG mode or not from env var
    DBG = True if int(os.getenv('DEBUG', default=0)) == 1 else False

    K_DENOM = 3

    # time constants
    BASE_INTERVAL = 10
    # TODO: add others as multiples of base interval
    MAINTAIN_INTERVAL = 9 * BASE_INTERVAL # 1.5m to let a few broadcast cycles go by
    STALENESS = 6 * 5 * BASE_INTERVAL # 5m to let several broadcast cycles go by

    # nameserver constants
    NAMESERVER_NAME = 'catalog.cse.nd.edu'
    NAMESERVER_PORT = 9097
    NAMESERVER_URL = '/query.json'
    NAMESERVER_WAIT = BASE_INTERVAL * 3

    # project name for nameserver
    NAMESERVER_TYPE = 'sPin'
    NAMESERVER_OWNER = 'jsulli28/jporubci'

    # storage locations
    PIN_DIR = 'pinned_files'
    CACHE_DIR = 'cached_files'
    META_DIR = 'meta'
    PIN_TRANS_BASE = 'pins'
    DEL_TRANS_BASE = 'dels'
    NAME_BASE = 'name'
    # temp file extension
    TEMP_EXTENSION = 'new'
    CKPT_EXTENSION = 'ckpt'
    LOG_EXTENSION = 'log'

    MAX_PIN_LOG_SIZE = 100
    MAX_DEL_LOG_SIZE = 5_000 # 102 chars, ~5000 records
    MAX_CACHE_SIZE = 10_000_000_000 # 10GB

    def __init__(self):

        # load or make new name
        self.name = self.get_name()
        
        # $UUID-$HASH table
        # UUID:HASH to HASH table
        self.pins = self.load_pins()
        # TODO: reconcile pins? i.e. if we don't have a certain file stored in PIN_DIR, remove it from our pins?

        # cache table
        # same as pins basically
        self.cache = {}
        shutil.rmtree(self.CACHE_DIR)
        os.mkdir(self.CACHE_DIR)

        # deletion table
        # just a list of UUID:HASHes - this makes dropping the back end easier when the size gets too big
        # TODO: do something more complex so lookups are faster?
        #TODO: checkpoint load
        self.dels = self.load_dels()

        # worldview table
        # records should look like: UUID:HASH -> [{node: xyz, lastheardfrom: 123}, ... ]
        self.world = collections.defaultdict(list)

        # peers table - to be initialized at first run of retrieve
        # records should look like: node uuid -> {host: , port: , lastheardfrom (? MAYBE ?)}
        self.peers = {}

        # port - to be initialized when server initialized
        self.port = None
        # host - same as above
        self.host = None

    # log a deletion
    # compress if necessary
    def log_del(self, object_id):

        log_location = f'{self.META_DIR}/{self.DEL_TRANS_BASE}.{self.LOG_EXTENSION}'

        if self.del_log_length > self.MAX_DEL_LOG_SIZE:
            # get from half to the end
            new_dels = self.dels[self.del_log_length // 2:]

            new_log_location = f'{self.META_DIR}/{self.DEL_TRANS_BASE}.{self.LOG_EXTENSION}.{self.TEMP_EXTENSION}'

            # open and write to new log
            with open(new_log_location, 'w') as new_log:
                new_log.writelines([f'{obj_id}\n' for obj_id in new_dels])
                new_log.flush()
                os.fsync(new_log.fileno())

            # close old log
            self.del_log.close()

            # replace with new
            os.replace(new_log_location, log_location)

            # open again
            self.del_log = open(log_location, 'a')

            # set dels to new_dels
            self.dels = new_dels
            self.del_log_length = len(self.dels)

            print('info: truncated deletes')

        try:
            self.del_log.write(object_id + '\n')
            self.del_log.flush()
            os.fsync(self.del_log.fileno())
            self.del_log_length += 1
            return True
        except OSError:
            print('error: could not add to deletion log')
            return False
        
    # load deletions from disk
    def load_dels(self):

        log_location = f'{self.META_DIR}/{self.DEL_TRANS_BASE}.{self.LOG_EXTENSION}'

        # attempt to load from log location into dels
        try:
            with open(log_location, 'r') as del_log:
                dels = [line.strip() for line in del_log]
        except OSError:
            print('error: could not load old deletion log')
            dels = []

        # open log location for appends
        self.del_log = open(log_location, 'a')
        self.del_log_length = len(dels)

        return dels
    
    # log a pin transaction
    # compress checkpoint if needed
    def log_pins(self, type, object_id):

        log_location = f'{self.META_DIR}/{self.PIN_TRANS_BASE}.{self.LOG_EXTENSION}'
        ckpt_location = f'{self.META_DIR}/{self.PIN_TRANS_BASE}.{self.CKPT_EXTENSION}'

        if self.pin_log_length > self.MAX_PIN_LOG_SIZE:

            # write out full pins to new ckpt
            with open(f'{ckpt_location}.{self.TEMP_EXTENSION}', 'w') as new_ckpt:
                to_write = json.dumps(self.pins)
                new_ckpt.write(to_write)
                new_ckpt.flush()
                os.fsync(new_ckpt.fileno())

            os.replace(f'{ckpt_location}.{self.TEMP_EXTENSION}', ckpt_location)

            self.pin_log.truncate(0)
            self.pin_log.seek(0)
            self.pin_log_length = 0

            print('info: compressed pin log to checkpoint')

        if type == 'add':
            log_line = f'ADD:{object_id}\n'
        else:
            log_line = f'DEL:{object_id}\n'

        try:
            self.pin_log.write(log_line)
            self.pin_log.flush()
            os.fsync(self.pin_log.fileno())
            self.pin_log_length += 1
            return True
        except OSError:
            print('error: could not add to pin log')
            return False
        
    # load pins from checkpoint and log
    def load_pins(self):

        log_location = f'{self.META_DIR}/{self.PIN_TRANS_BASE}.{self.LOG_EXTENSION}'
        ckpt_location = f'{self.META_DIR}/{self.PIN_TRANS_BASE}.{self.CKPT_EXTENSION}'

        # attempt to load checkpoint
        try:
            ckpt = open(ckpt_location, 'r')
            ckpt_present = True
        except OSError:
            with open(ckpt_location, 'w') as new_ckpt:
                new_ckpt.write('{}\n')
                new_ckpt.flush()
                os.fsync(new_ckpt.fileno())
            ckpt_present = False

        # attempt to load log
        try:
            log = open(log_location, mode='r')
            log_present = True
        except OSError:
            log_present = False

        # check if ckpt newer
        if (ckpt_present and log_present) and (os.stat(ckpt.fileno()).st_mtime_ns > os.stat(log.fileno()).st_mtime_ns):
            ckpt_newer = True
        else:
            ckpt_newer = False

        self.pin_log_length = 0

        # if no ckpt, make an empty table
        if not ckpt_present:
            pins = {}
        else:
            pins = json.loads(ckpt.read())

            # if checkpoint older than txn, replay
            if not ckpt_newer and log_present:
                for line in log:
                    self.pin_log_length += 1

                    line = line.strip()
                    elements = line.split(':')

                    if elements[0] == 'ADD':
                        pins[f'{elements[1]}:{elements[2]}'] = elements[2]
                    else: # DEL
                        try:
                            del pins[f'{elements[1]}:{elements[2]}']
                        except KeyError:
                            pass

        # clean up
        if ckpt_present:
            ckpt.close()
        if log_present:
            log.close()

        # open log for appending
        self.pin_log = open(log_location, 'a')

        return pins

    def get_name(self):
        # track whether we need to write out
        store = False

        # attempt to load ./meta/name
        try:
            with open(f'{self.META_DIR}/{self.NAME_BASE}', 'r') as name_file:
                name = name_file.readline().strip()
        except OSError:
            name = None
            store = True

        # if it exists and is valid, use it as the name for this instance
        if name:
            try:
                check_uuid = uuid.UUID(name)
                name = str(check_uuid)
            except ValueError:
                name = str(uuid.uuid4())
                store = True
        else:
            name = str(uuid.uuid4())
            store = True

        # atomically store name if needed
        if store:
            name_old_path = f'{self.META_DIR}/{self.NAME_BASE}'
            name_new_path = f'{self.META_DIR}/{self.NAME_BASE}.new'
            with open(name_new_path, 'w') as name_file:
                print(name, file=name_file, flush=True)
                os.fsync(name_file.fileno())
            os.replace(name_new_path, name_old_path)
            os.remove(name_new_path)

        return name # return string version of uuid loaded or created

    # update nameserver
    async def update_nameserver(self):
        msg = {
            'type': self.NAMESERVER_TYPE,
            'owner': self.NAMESERVER_OWNER,
            'port': self.port,
            'uuid': self.name,
        }
        msg_encoded = json.dumps(msg).encode()

        # keep going forever
        while True:
            """# send message to the nameserver
            # TODO: try/except??? - can't find the exception we'd need to catch... don't want to just catch everything, that'd grab CTRL-C and stuff
            _, write = await asyncio.open_connection(self.NAMESERVER_NAME, self.NAMESERVER_PORT, family=socket.AF_INET, proto=socket.SOCK_DGRAM)

            if self.DBG: pprint.pprint(msg)

            write.write(msg_encoded)
            await write.drain()

            write.close()
            await write.wait_closed()"""

            print('info: updating nameserver about self')

            if self.DBG: pprint.pprint(msg)

            # TODO: consider using the above async/await form of this... possibly unnecessary since it's a single, quick connection
            # send message, context handler to close
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
                # avoid setup, use sendto
                udp_socket.sendto(msg_encoded, (self.NAMESERVER_NAME, self.NAMESERVER_PORT))

            # wait the required amount of time
            await asyncio.sleep(self.NAMESERVER_WAIT)

    # retrieve from nameserver
    async def retrieve_peers(self):
        # keep going forever
        while True:

            print('info: retrieving peers from nameserver')

            # async retrieval of the JSON
            async with aiohttp.ClientSession() as session:
                async with session.get(f'http://{self.NAMESERVER_NAME}:{self.NAMESERVER_PORT}{self.NAMESERVER_URL}') as resp:
                    # TODO: error check
                    nameserver_json = await resp.json(content_type=None) # disable content type check, nameserver gives text even though it's json

            # find our project in nameserver json response
            # TODO: fix the fact that this will currently find a crapload of stuff if there's been a lot of churn at the nameserver recently
            #self.peers = [entry for entry in nameserver_json if entry.get('type', '') == self.NAMESERVER_TYPE and entry.get('uuid') != self.ID]
            now = time.time()
            all_peers = [entry for entry in nameserver_json if entry.get('type', '') == self.NAMESERVER_TYPE and entry.get('uuid') != self.name and (now - entry.get('lastheardfrom') < 300)]

            if self.DBG: pprint.pprint(all_peers)

            duplicates = {}
            for peer in all_peers:
                if peer['uuid'] in duplicates:
                    duplicates[peer['uuid']].append(peer)
                else:
                    duplicates[peer['uuid']] = [peer]

            dedup = [max(dupes, key=lambda k: k['lastheardfrom']) for dupes in duplicates.values()]
            self.peers = {record['uuid'] : record for record in dedup}
                
            if self.DBG:     
                print('received following peers:')
                pprint.pprint(self.peers)

            # broadcast to all peers
            await self.broadcast(list(self.peers.values()))

            # wait the required amount of time
            await asyncio.sleep(self.NAMESERVER_WAIT)

    # maintain the various data structures and call cleanup functions as needed
    async def maintain(self):
        
        # hack to let things fill up before maintain starts running
        await asyncio.sleep(self.NAMESERVER_WAIT + 5)

        # keep going forever
        while True:

            print('info: maintenance running')

            # remove anything too old from worldview
            now = time.time()
            new_world = collections.defaultdict(list)
            for obj, known_pins in self.world.items():
                non_stale = [pin for pin in known_pins if (now - pin['lastheardfrom'] < self.STALENESS)]
                new_world[obj] = non_stale

            self.world = new_world

            print('info: maintenance updated worldview')

            # calculate k
            k = math.ceil(len(self.peers) / self.K_DENOM)

            # check for too many or too few pins
            for obj, known_pins in self.world.items():

                if self.pins.get(obj):
                    count = len(known_pins)
                    pins = [curr['node'] for curr in known_pins]
                    pins.append(self.name)
                    not_pins = [node for node in self.peers.keys() if node not in pins]

                    if count > k:
                        to_drop = pin_funcs.drop_pin(self.name, pins)
                        if to_drop and to_drop != self.name:
                            node = self.peers[to_drop]
                            #pprint.pprint(self.peers)
                            name = f'''{node['name']}:{node['port']}'''
                            print(f'info: maintenance instructing {name} to drop {obj}')
                            await self.notify_drop(name, obj)

                    if count < k:
                        to_add = pin_funcs.add_pin(self.name, pins, not_pins)
                        if to_add and to_add != self.name:
                            node = self.peers[to_add]
                            #pprint.pprint(self.peers)
                            name = f'''{node['name']}:{node['port']}'''
                            print(f'info: maintenance instructing {name} to pin {obj}')
                            await self.notify_pin(name, obj)

            print('info: maintenance verified pin counts')

            # delete oldest files in cache if too large
            # TODO: just gonna let this be for now... hopefullly it won't get too big
            #if True:
            #    print('info: maintenance cleaned excess cache')

            # wait the required amount of time
            await asyncio.sleep(self.MAINTAIN_INTERVAL)

    # broadcast information to other peers
    async def broadcast(self, peers):

        # prep pins for sending
        payload = [{'object': obj, 'node': self.name} for obj in self.pins]

        for peer in peers:

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                print(f'''info: posting information to http://{peer['name']}:{peer['port']}/info:''')

                if self.DBG: pprint.pprint(self.pins)

                try:
                    await session.post(f'''http://{peer['name']}:{peer['port']}/info''', json=payload)
                    print(f'''info: posting information to http://{peer['name']}:{peer['port']}/info succeeded''')
                except:
                    # TODO: differentiate more?
                    print(f'''info: posting information to http://{peer['name']}:{peer['port']}/info failed: peer likely unavailable''')

    # ADD operation
    async def add_handler(self, request):
        identifier = request.match_info['identifier']

        hash = identifier.split(':')[1]

        print(f'receiving file from client with identifier of {identifier}')

        write_success = False
        recv_success = False

        async for field in (await request.multipart()):

            if field.name == 'data':
                recv_success = True
                with open(f'{self.PIN_DIR}/{hash}', 'wb') as file:
                    while True:
                        chunk = await field.read_chunk()
                        if not chunk:
                            break
                        file.write(chunk)

                    # ensure written out
                    file.flush()
                    os.fsync(file.fileno())
                    write_success = True
            else:
                continue # skip if not data field

        # add to pins dict
        self.log_pins('ADD', identifier)
        self.pins[identifier] = hash

        if write_success and recv_success:
            return web.Response()
        elif not recv_success:
            return web.Response(status=400)
        else: # failed receive data
            return web.Response(status=500)

    # INFO operation
    async def info_handler(self, request):

        recv_time = time.time()
        payload = await request.json()

        print(f'''info: receiving information from peer''')
        if self.DBG:
            print(f'full payload:')
            pprint.pprint(payload)
        
        for record in payload:
            
            if record['object'] in self.dels:
                address = f'''{self.peers[record['node']]['name']}:{self.peers[record['node']]['port']}'''
                await self.notify_deletion(address, record['object'])
            else:
                # add record to world
                self.world[record['object']].append({'node': record['node'], 'lastheardfrom': recv_time})

        return web.Response()
    
    # DEL operation
    async def del_handler(self, request):
        identifier = request.match_info['identifier']

        # DROP requests from others will have a body, others won't
        # kinda hacky but
        if request.body_exists:
            drop = True
        else:
            drop = False

        print(f'servicing deletion request for object id: {identifier}')

        # add to dels
        # TODO: CHECKPOINT
        if not drop and identifier not in self.dels: 
            self.dels.append(identifier)

        # delete from pins and cache
        try:
            self.log_pins('DEL', identifier)
            del self.pins[identifier]
        except KeyError:
            pass
        if not drop:
            try:
                del self.cache[identifier]
            except KeyError:
                pass

        # delete file if no other pins refer to it
        hash = identifier.split(':')[1]
        if hash not in self.pins.values():
            try:
                os.remove(f'{self.PIN_DIR}/{hash}')
            except FileNotFoundError:
                pass
        if not drop and hash not in self.cache.values():
            try:
                os.remove(f'{self.CACHE_DIR}/{hash}')
            except FileNotFoundError:
                pass

        # notify others if needed
        #if not drop and self.world.get(identifier):
        #    for node in self.world[identifier]:
        #        address = f'''{self.peers[node['node']]['name']}:{self.peers[node['node']]['port']}'''
        #        await self.notify_deletion(address, identifier)

        return web.Response()


    # GET operation
    async def get_handler(self, request):
        identifier = request.match_info['identifier']

        hash = identifier.split(':')[1]

        if self.pins.get(identifier):
            print(f'providing client with file with identifier of {identifier}')
            return web.FileResponse(f'{self.PIN_DIR}/{hash}')
        elif self.world.get(identifier):
            print(f'getting file with identifier of {identifier}')
            
            # pick a peer to get from
            node = random.choice(self.world[identifier])

            # TODO: retry
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f'http://{node}/get/{identifier}') as response:
                        data = await response.read()

                        # write to cache
                        with open(f'{self.CACHE_DIR}/{hash}', 'wb') as file:
                            file.write(data)
                            file.flush()
                            os.fsync(file.fileno())

                        return web.FileResponse(f'{self.CACHE_DIR}/{hash}')
            except:
                web.Response(status=404)
        else:
            return web.Response(status=404)
    
    # deletion notifier
    # where node is the name of the node and object is the UUID:HASH combo
    async def notify_deletion(self, node, object):

        async with aiohttp.ClientSession() as session:
            print(f'''notifying {node} that {object} has been deleted''')

            try:
                await session.post(f'http://{node}/del/{object}')
            except:
                pass # TODO: don't ignore failed? it should be fine because it'll come back around but

    # drop notifier
    async def notify_drop(self, node, object):

        async with aiohttp.ClientSession() as session:
            print(f'''notifying {node} that {object} must be dropped''')

            try:
                await session.post(f'http://{node}/del/{object}', data='drop')
            except:
                pass # TODO: don't ignore failed? it should be fine because it'll come back around but

    # add notifier/uploader
    async def notify_pin(self, node, object):

        hash = object.split(':')[1]

        async with aiohttp.ClientSession() as session:
            print(f'''notifying {node} that {object} must be pinned''')

            try:
                with open(f'{self.PIN_DIR}/{hash}', 'rb') as file:
                    await session.post(f'http://{node}/add/{object}', data=file)
            except:
                pass # TODO: don't ignore failed? it should be fine because it'll come back around but

    # server main loop
    async def serve(self):

        # set up app
        app = web.Application()
        
        app.add_routes([web.post('/info', self.info_handler),
                web.post('/add/{identifier}', self.add_handler),
                web.post('/del/{identifier}', self.del_handler),
                web.get('/get/{identifier}', self.get_handler)])

        # set up aiohttp server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=socket.getfqdn(), port=0, reuse_port=True)
        await site.start()

        # TODO: hacky... is there a way around this?
        self.port = site._server.sockets[0].getsockname()[1]
        self.host = socket.getfqdn()
        print(f'{self.name} @ {self.host}:{self.port}')

        # run other tasks
        await asyncio.gather(self.update_nameserver(), self.retrieve_peers(), self.maintain())

        # wait forever
        await asyncio.Event().wait()

if __name__ == '__main__':
    s = sPinServer()
    asyncio.run(s.serve())