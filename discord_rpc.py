import asyncio
import enum
import json
import os
import pickle
import re
import sys
import tempfile
import time
import traceback
from typing import Optional

import aiofiles
import emoji
import psutil
from discoIPC.ipc import DiscordIPC
from moviepy.video.io.VideoFileClip import VideoFileClip
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from rapidfuzz import fuzz

from lastfm import LastFM
from utils.spotify import SpotifyClient

yt_playlist_regex = re.compile(r'(?:list=)?([a-zA-Z0-9_-]+)')

yt_video_regex = re.compile(r'(?:^|(?<=\W))[-a-zA-Z0-9_]{11}(?:$|(?=\W))')

players = {
            "potplayermini64.exe": {
                "name": "PotPlayer (x64)",
                "icon": "https://upload.wikimedia.org/wikipedia/commons/e/e0/PotPlayer_logo_%282017%29.png"
            },
            "potplayermini.exe": {
                "name": "PotPlayer",
                "icon": "https://upload.wikimedia.org/wikipedia/commons/e/e0/PotPlayer_logo_%282017%29.png"
            },
            "mpc-hc64.exe": {
                "name": "Media Player Classic HC-x64",
                "icon": "https://upload.wikimedia.org/wikipedia/commons/7/76/Media_Player_Classic_logo.png"
            },
            "mpc-hc.exe": {
                "name": "Media Player Classic HC",
                "icon": "https://upload.wikimedia.org/wikipedia/commons/7/76/Media_Player_Classic_logo.png"
            },
            "foobar2000.exe": {
                "name": "foobar2000",
                "icon": "https://i.sstatic.net/JowsQ.jpg"
            },
            "vlc.exe": {
                "name": "VLC Player",
                "icon": "https://cdn1.iconfinder.com/data/icons/metro-ui-dock-icon-set--icons-by-dakirby/512/VLC_Media_Player.png"
            },
            "winamp.exe": {
                "name": "Winamp",
                "icon": "https://iili.io/dsKTaUB.md.png"
            },
            "aimp.exe": {
                "name": "AIMP",
                "icon": "https://iili.io/dsKpuSV.md.png"
            },
            "musicbee.exe": {
                "name": "MusicBee",
                "icon": "https://iili.io/dsf9KQe.png"
            },
            "mediamonkeyengine.exe": {
                "name": "Media Monkey",
                "icon": "https://iili.io/dsfaPs9.png",
            },
            "kmplayer.exe": {
                "name": "KM Player",
                "icon": "https://cdn6.aptoide.com/imgs/b/4/8/b48d248dc9514b23279b87e3e3c70c7d_icon.png?w=512",
            },

            # os players do windows tem um problema que ocasionalmente fica lendo todos os arquivos de música
            # do pc o que faz com que as informações de arquivos de músicas em uso pelo processo confunda a música ativa no momento.

            # "wmplayer.exe": {
            #    "name": "Windows Media Player (Legacy)",
            #    "icon": "https://iili.io/dsfd18x.png"
            # },
            # "microsoft.media.player.exe": {
            #    "name": "Microsoft Media Player",
            #    "icon": "https://iili.io/dsfqJvs.md.png"
            # }
        }

class ActivityType(enum.Enum):
    playing = 0
    listening = 2
    watching = 3
    competing = 5

class MyDiscordIPC(DiscordIPC):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _send(self, opcode, payload):

        encoded_payload = self._encode(opcode, payload)

        try:
            if self.platform == 'windows':
                self.socket.write(encoded_payload)
                try:
                    self.socket.flush()
                except OSError:
                    raise IPCError(f'Não foi possivel enviar dados ao discord via IPC.', client=self)
            else:
                self.socket.send(encoded_payload)
        except Exception as e:
            raise IPCError(f'Não foi possivel enviar dados ao discord via IPC | Erro: {repr(e)}.', client=self)

    def _get_ipc_path(self, id=0):
        # credits: pypresence https://github.com/qwertyquerty/pypresence/blob/31718fb442e563f879160c16e0215c7c1fa16f23/pypresence/utils.py#L25
        ipc = f"discord-ipc-{id}"

        if sys.platform in ('linux', 'darwin'):
            tempdir = (os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir())
            paths = ['.', 'snap.discord', 'app/com.discordapp.Discord', 'app/com.discordapp.DiscordCanary']
        elif sys.platform == 'win32':
            tempdir = r'\\?\pipe'
            paths = ['.']
        else:
            return

        for path in paths:
            full_path = os.path.abspath(os.path.join(tempdir, path))
            if sys.platform == 'win32' or os.path.isdir(full_path):
                for entry in os.scandir(full_path):
                    if entry.name.startswith(ipc) and os.path.exists(entry):
                        return entry.path

class IPCError(Exception):

    def __init__(self, error, client: MyDiscordIPC):
        self.error = error
        self.client = client

    def __repr__(self):
        return self.error

class RpcRun:

    def __init__(self):
        self.playlist_id: Optional[str] = None
        self.playlist_name: Optional[str] = None
        self.process: Optional[psutil.Process] = None
        self.author: Optional[str] = None
        self.track_name: Optional[str] = None
        self.track_number: Optional[str] = None
        self.video_id: Optional[str] = None
        self.track_duration: Optional[int] = None
        self.rpc_client = None
        self.player_name = None
        self.player_icon = None
        self.current_file = ""
        self.user_id = None
        self.username = None
        self.spotify = SpotifyClient()
        self.activity_type = ActivityType.listening.value
        self.scrobble_task: Optional[asyncio.Task] = None
        self.loop = None

        self.last_fm = None

        if (lastfm_key:=os.getenv("LASTFM_KEY")) and (lastfm_secret:=os.getenv("LASTFM_SECRET")):
            self.last_fm = LastFM(lastfm_key, lastfm_secret)
        else:
            print("Sistema de scrobble via last.fm desativado.")

        try:
            with open("./lastfm_ignore_playlists.txt") as f:
                self.ignore_playlists = set([p for p in yt_playlist_regex.findall(f.read())])
        except FileNotFoundError:
            traceback.print_exc()
            self.ignore_playlists = set()


    async def clear_info(self):
        self.playlist_id = None
        self.playlist_name = None
        self.author = None
        self.track_name = None
        self.video_id = None
        self.current_file = None
        try:
            self.rpc_client.clear()
        except AttributeError:
            pass
        await asyncio.sleep(15)

    async def start_scrobble(self, query, duration: int):

        if not self.last_fm or not self.user_id:
            return

        try:
            async with aiofiles.open("./.lastfm_keys.json", encoding='utf-8') as f:
                users = json.loads(await f.read())
        except FileNotFoundError:
            users = {}

        if not (fmdata:=users.get(self.user_id)):
            print("Scrobble ignorado devido ao usuário não ter autenticado uma conta no last.fm (use o start_lastfm_auth pra isso).")
            self.save_scrobble(query, self.user_id)
            return

        print(f"Iniciando scrobble: {query}")

        await asyncio.sleep(int(duration/3))

        if (data:=self.last_fm.cache.get(query)) is None:

            try:
                result = await self.spotify.track_search(query)
            except Exception:
                traceback.print_exc()
            else:

                tags = ("mix", "remix", "extended")

                if result:

                    for t in result["tracks"]["items"]:

                        if any(t for t in tags if t in query) and not any(g for g in tags if g in t["name"].lower()):
                            continue

                        string_check = t["name"].lower() + " - " + ", ".join(a["name"].lower() for a in t["artists"] if a["name"].lower() not in t["name"].lower())

                        if fuzz.token_sort_ratio(string_check, query) > 70:

                            data = {
                                "name": t["name"],
                                "artist": t["artists"][0]["name"],
                                "album": t["album"]["name"],
                                "duration": t["duration_ms"] / 1000
                            }

                            self.last_fm.cache[query] = data

                            break

        if not data:
            self.last_fm.cache[query] = {}
            print(f"Scrobble ignorado: {query}")
            self.save_scrobble(query, self.user_id)
            return

        await self.last_fm.track_scrobble(
            artist=data["artist"], track=data["name"], album=data["album"], duration=data["duration"],
            session_key=fmdata["key"]
        )

        print(f"Scroble efetuado com sucesso: {query}")

    def save_scrobble(self, query: str, user_id: str):

        os.makedirs("./scrobbles", exist_ok=True)

        try:
            with open(f"./scrobbles/{user_id}.pkl", "rb") as f:
                scrobbles = pickle.load(f)
        except FileNotFoundError:
            scrobbles = []

        scrobbles.append([query, time.time()])

        with open(f"./scrobbles/{user_id}.pkl", "wb") as f:
            pickle.dump(scrobbles, f)

    async def start_loop(self):

        while True:

            try:
                if not self.process or not self.process.is_running():
                    if (p:=self.get_process(file_result=True)) is None:
                        await self.clear_info()
                        continue

                elif (p:=self.check_process(self.process)) is None:
                    await self.clear_info()
                    continue

                if not self.loop:
                    self.loop = asyncio.get_event_loop()

                if not self.rpc_client:
                    for i in range(10):
                        try:
                            rpc = MyDiscordIPC("1287237467400962109", pipe=i)
                            await self.loop.run_in_executor(None, lambda: rpc.connect())
                            self.rpc_client = rpc
                            break
                        except Exception:
                            continue

                    if not self.rpc_client:
                        await asyncio.sleep(15)
                        continue

                    try:
                        self.username = self.rpc_client.data["data"]["user"]["username"]
                        self.user_id = str(self.rpc_client.data["data"]["user"]["id"])
                        print(f'Usuário conectado: {self.username} [{self.user_id}]')
                    except KeyError:
                        self.rpc_client = None
                        continue

                if p == self.current_file:
                    await asyncio.sleep(15)
                    continue

                # Contagem de caracteres do botão consomem o dobro do limite de um caracter normal
                playlist_limit = 25 if emoji.emoji_count(self.playlist_name) < 1 else 18

                # testes
                try:
                    with open("playlist_info.json") as f:
                        playlist_data = json.load(f)
                except FileNotFoundError:
                    playlist_data = {}

                payload = {
                    "details": self.track_name,
                    "state": f"By: {', '.join(self.author.split(', ')[:4])}",
                    "assets": {
                        "large_image": f"https://img.youtube.com/vi/{self.video_id}/default.jpg",
                        "large_text": f"Via: {self.player_name}",
                        "small_image": self.player_icon,

                    },
                    "type": self.activity_type,
                    "buttons": [
                        {
                            "label": "Listen" if self.activity_type == ActivityType.listening.value else "Watch" + " on Youtube",
                            "url": f"https://www.youtube.com/watch?v={self.video_id}&list={self.playlist_id}{playlist_data.get(self.video_id, '')}"
                        },
                        {
                            "label": self.playlist_name[:playlist_limit] if len(self.playlist_name[:playlist_limit]) > 13 else f"Playlist: {self.playlist_name[:playlist_limit]}",
                            "url": f"https://www.youtube.com/playlist?list={self.playlist_id}"
                        },
                    ]
                }

                if self.track_number:
                    try:
                        track_number = int(self.track_number.split("/")[0])-1
                    except:
                        track_number = self.track_number
                    payload["buttons"][0]["url"] += f"&index={track_number}"
                    payload["buttons"][0]["label"] += f" ({self.track_number})"

                try:
                    self.rpc_client.update_activity(payload)
                    self.current_file = p

                    if self.playlist_id in self.ignore_playlists:
                        print(f"Scrobble ignorado: {self.track_name} - {self.author} [playlist: https://www.youtube.com&list={self.playlist_id}]")
                    else:
                        try:
                            self.scrobble_task.cancel()
                        except:
                            pass
                        if self.author.endswith(" - topic") and not self.author.endswith(
                                "Release - topic") and not self.track_name.startswith(self.author[:-8]):
                            query = f"{self.author} - {self.track_name}"
                        else:
                            query = self.track_name.lower() if len(self.track_name) > 12 else (
                                f"{self.author} - {self.track_name}".lower())

                        self.scrobble_task = self.loop.create_task(
                            self.start_scrobble(query=query, duration=self.track_duration)
                        )
                except Exception:
                    traceback.print_exc()
                    self.rpc_client = None
                    await asyncio.sleep(30)

            except Exception:
                traceback.print_exc()
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(15)

    def check_process(self, proc: psutil.Process):

        for o in proc.open_files():

            if self.current_file == o.path:
                return o.path

            if o.path.endswith((".mp3", ".mp4")) and (yt_id := yt_video_regex.search(o.path)):
                try:
                    with open(f"{os.path.dirname(o.path)}/playlist_info.json") as f:
                        playlist_info = json.load(f)
                except FileNotFoundError:
                    continue

                self.playlist_name = playlist_info["title"]
                self.playlist_id = playlist_info["id"]

                if o.path.endswith(".mp3"):
                    tags = MP3(o.path, ID3=EasyID3)
                    self.track_name = tags["title"][0]
                    self.author = tags["artist"][0]
                    self.track_duration = tags.info.length
                    self.activity_type = ActivityType.listening.value
                    try:
                        self.track_number = tags.get("tracknumber")[0]
                    except:
                        self.track_number = None
                else:
                    tags = MP4(o.path)
                    self.track_name = tags.get("\xa9nam")[0]
                    self.author = tags.get("\xa9ART")[0]
                    self.track_number = tags.get('trac')[0]
                    self.track_duration = VideoFileClip(o.path).duration
                    self.activity_type = ActivityType.watching.value

                self.video_id = yt_id.group()
                self.process = proc

                player_info = players.get(proc.name().lower())

                self.player_name = player_info["name"]
                self.player_icon = player_info["icon"]
                return o.path

    def get_process(self, file_result=False):

        for proc in psutil.process_iter(['pid', 'name']):

            if [p for p in players if p.lower() in (proc.name()).lower()]:

                if not (f:=self.check_process(proc)):
                    continue

                return f if file_result else proc

        self.process = None


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(RpcRun().start_loop())
