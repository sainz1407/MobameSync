# -*- coding: utf-8 -*-

# Importing Section
import sys
import os
import time
import logging
import re
import json
import requests
from queue import Queue
sys.path.append('../')
from database.config import *
import main
from urllib.parse import urlparse
from pydub import AudioSegment
import httpx
from threading import Thread
from datetime import datetime
from telebot.apihelper import ApiTelegramException

# GLOBAL CONFIG SECTION
apihelper.SESSION_TIME_TO_LIVE = 5 * 60
mobamebot = telebot.TeleBot(teletoken())
syncstate = False
SYNC_INTERVAL_SECONDS = 10
auto_scheduler_enabled = True

# Message queue untuk menghindari rate limiting
message_queue = Queue()

def _message_sender_worker():
    while True:
        try:
            chat_id, text, disable_preview = message_queue.get(timeout=1)
        except Exception:
            time.sleep(0.2)
            continue

        for attempt in range(6):
            try:
                mobamebot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=disable_preview)
                # pause antar pesan untuk avoid burst
                time.sleep(1.0)
                break
            except ApiTelegramException as e:
                # handle rate-limit: "Too Many Requests: retry after <n>"
                if getattr(e, "error_code", None) == 429:
                    m = re.search(r'retry after (\d+)', str(e))
                    wait = int(m.group(1)) if m else 30
                    logging.warning(f"Telegram rate limit — waiting {wait}s (attempt {attempt+1})")
                    time.sleep(wait + 1)
                    continue
                logging.error(f"Telegram API error: {e}")
                break
            except Exception as e:
                logging.error(f"Unexpected send error: {e}. retrying...")
                time.sleep(5)
        message_queue.task_done()

# Jalankan worker di background
Thread(target=_message_sender_worker, daemon=True).start()

# Flag untuk menandai bahwa scheduler yang menghentikan sync
_auto_stopped_by_scheduler = False

# fungsi helper untuk start/stop background sync
def _start_sync_background(notify_owner=False):
  global syncstate, _auto_stopped_by_scheduler
  if syncstate:
    return
  syncstate = True
  _auto_stopped_by_scheduler = False
  logging.info("Auto-start sync (scheduler).")

  def _runner():
    while syncstate:
      try:
        syncer()
        time.sleep(SYNC_INTERVAL_SECONDS)
      except Exception as e:
        logging.error(f"Error di loop sinkronisasi: {e}")
        time.sleep(30)
  Thread(target=_runner, daemon=True).start()

def _stop_sync_background():
  global syncstate, _auto_stopped_by_scheduler
  if not syncstate:
    return
  syncstate = False
  _auto_stopped_by_scheduler = True
  logging.info("Auto-stop sync (scheduler).")

# scheduler untuk toggle sync pada jam 23:00 - 04:00
def _auto_sync_scheduler():
  global syncstate, _auto_stopped_by_scheduler, auto_scheduler_enabled
  while True:
    try:
      if not auto_scheduler_enabled:
        time.sleep(30)
        continue
      now = datetime.now()
      hour = now.hour
      in_window = (hour >= 23 or hour < 4)
      
      if in_window and syncstate:
        _stop_sync_background()
      elif not in_window and _auto_stopped_by_scheduler and not syncstate:
        _start_sync_background()
        _auto_stopped_by_scheduler = False
    except Exception as e:
      logging.error(f"Scheduler error: {e}")
    time.sleep(30)

# Jalankan scheduler di background
Thread(target=_auto_sync_scheduler, daemon=True).start()

# SOME FUNCTIONS SECTION
def message_sender_and_recorder(grup, servermsgdb, localmsgdb):
  def _send_with_retry(send_func, label, max_attempt=6):
    for attempt in range(1, max_attempt + 1):
      try:
        send_func()
        time.sleep(1.0)
        return True
      except ApiTelegramException as e:
        if getattr(e, "error_code", None) == 429:
          m = re.search(r'retry after (\d+)', str(e))
          wait = int(m.group(1)) if m else 30
          logging.warning(f"{label} rate limit, wait {wait}s (attempt {attempt}/{max_attempt})")
          time.sleep(wait + 1)
          continue
        logging.error(f"{label} Telegram API error: {e}")
        return False
      except Exception as e:
        logging.error(f"{label} unexpected send error: {e} (attempt {attempt}/{max_attempt})")
        time.sleep(2)
    logging.error(f"{label} gagal dikirim setelah {max_attempt} percobaan")
    return False

  with open(localmsgdb, 'r+') as localmessage:
    config = configfile()
    membersdb = config['Sakamichi_App'][grup]['telegram_services']['members']
    localmsgid = json.load(localmessage)
    
    for servermessage in servermsgdb:
      if servermessage.get('state') != 'published' or servermessage['id'] in localmsgid:
        continue
      
      for memberdb in membersdb:
        if servermessage['group_id'] != memberdb['id']:
          continue
        
        message_sent = False
        try:
          # Text messages
          if servermessage.get('type') == 'text':
            text = f"{re.sub(r'[%％]{1,}', 'Sainz ', servermessage['text'])}\n\n#{memberdb['name'].replace(' ','')}"
            message_queue.put((telechatid(), text, True))
            message_sent = True
          
          # Picture with text
          elif servermessage.get('type') == 'picture' and 'text' in servermessage:
            caption = f"{re.sub(r'[%％]{1,}', 'Sainz ', servermessage['text'])}\n\n#{memberdb['name'].replace(' ','')}"
            message_sent = _send_with_retry(
              lambda: mobamebot.send_photo(chat_id=telechatid(), photo=servermessage['file'], caption=caption),
              f"[{grup}] photo-with-text #{memberdb['name'].replace(' ','')}"
            )
          
          # Picture without text
          elif servermessage.get('type') == 'picture' and 'text' not in servermessage:
            caption = f"#{memberdb['name'].replace(' ','')}"
            message_sent = _send_with_retry(
              lambda: mobamebot.send_photo(chat_id=telechatid(), photo=servermessage['file'], caption=caption),
              f"[{grup}] photo #{memberdb['name'].replace(' ','')}"
            )
          
          # Voice messages
          elif servermessage.get('type') == 'voice':
            m4a_path = ''
            mp3_path = ''
            try:
              parsed_url = urlparse(servermessage['file'])
              filename = os.path.basename(parsed_url.path)
              m4a_path = f"{ROOT_DIR}{tempdir}/{filename}"
              mp3_path = m4a_path.replace('.m4a', '.mp3')
              os.makedirs(os.path.dirname(m4a_path), exist_ok=True)
              response = httpx.get(servermessage['file'], timeout=30)
              if response.status_code != 200 or not response.content:
                logging.error(f"Failed to download voice file: HTTP {response.status_code}")
                continue
              with open(m4a_path, 'wb') as f:
                f.write(response.content)
              # Convert m4a to mp3
              AudioSegment.from_file(m4a_path, format="m4a").export(mp3_path, format="mp3")
              # Send audio
              def _send_audio_file():
                with open(mp3_path, 'rb') as audio:
                  mobamebot.send_audio(chat_id=telechatid(), audio=audio, caption=f"#{memberdb['name'].replace(' ','')}")
              message_sent = _send_with_retry(_send_audio_file, f"[{grup}] voice #{memberdb['name'].replace(' ','')}")
            except Exception as e:
              logging.error(f"Error processing voice message: {e}")
            finally:
              for path in [m4a_path, mp3_path]:
                if path and os.path.exists(path):
                  os.remove(path)
          
          # Video messages
          elif servermessage.get('type') == 'video':
            requestvideo = httpx.get(url=servermessage['file'])
            content_length = int(requestvideo.headers.get('content-length', 0))
            if content_length <= 18000000:
              message_sent = _send_with_retry(
                lambda: mobamebot.send_video(chat_id=telechatid(), video=servermessage['file'], caption=f"#{memberdb['name'].replace(' ','')}"),
                f"[{grup}] video #{memberdb['name'].replace(' ','')}"
              )
            else:
              parsed_url = urlparse(servermessage['file'])
              filename = os.path.basename(parsed_url.path)
              videopath = f"{ROOT_DIR}{tempdir}/{filename}"
              
              os.makedirs(os.path.dirname(videopath), exist_ok=True)
              with open(videopath, 'wb') as tempvid:
                tempvid.write(requestvideo.content)
              
              time.sleep(5)
              try:
                def _send_large_video():
                  with open(videopath,'rb') as videofile:
                    mobamebot.send_video(chat_id=telechatid(), video=videofile, caption=f"#{memberdb['name'].replace(' ','')}", timeout=None)
                message_sent = _send_with_retry(_send_large_video, f"[{grup}] large-video #{memberdb['name'].replace(' ','')}")
              finally:
                os.remove(videopath)
          else:
            message_sent = True
        
        except Exception as e:
          logging.error(f"Error processing {servermessage.get('type')} message: {e}")
          message_sent = False

        if message_sent:
          if servermessage['id'] not in localmsgid:
            localmsgid.append(servermessage['id'])
        else:
          logging.warning(f"Message ID {servermessage['id']} belum berhasil dikirim, akan dicoba lagi di sync berikutnya")
        break
      
      if not any(servermessage['group_id'] == memberdb['id'] for memberdb in membersdb):
        logging.warning(f"Member dengan group_id={servermessage['group_id']} tidak ditemukan di service {grup}")
    
    localmessage.seek(0)
    json.dump(localmsgid, localmessage, indent=2)
    localmessage.truncate()

def syncer():
  nogi = []
  nogi.clear()
  grouplister('nogizaka46',nogi)
  if nogi:
    try:
      main.Nogizaka.stream_timelines(nogi)
      message_sender_and_recorder('nogizaka46',main.nogitodaymessages, nogilocalmsg)
    except Exception as e:
      logging.error(f"syncer(nogizaka) error: {e}")
      time.sleep(5)
  
  saku = []
  saku.clear()
  grouplister('sakurazaka46',saku)
  if saku:
    try:
      main.Sakurazaka.stream_timelines(saku)
      message_sender_and_recorder('sakurazaka46',main.sakutodaymessages, sakulocalmsg)
    except Exception as e:
      logging.error(f"syncer(sakurazaka) error: {e}")
      time.sleep(5)
  
  hina = []
  hina.clear()
  grouplister('hinatazaka46',hina)
  if hina:
    try:
      main.Hinatazaka.stream_timelines(hina)
      message_sender_and_recorder('hinatazaka46',main.hinatodaymessages, hinalocalmsg)
    except Exception as e:
      logging.error(f"syncer(hinata) error: {e}")
      time.sleep(5)

# BUTTON CONFIG SECTION
updatedb      = Button(button_data={"Update Database":"updatedb"}).button
togglegroup   = Button(button_data={"Group Toggle":"grouptoggleservice"}).button
togglemember  = Button(button_data={"Member Toggle":"membertoggleservice"}).button
backtoconfig  = Button(button_data={"Kembali":"backtoconfig"}).button
cancel        = Button(button_data={"Batal":"cancel"}).button

# KEYBOARD CREATOR
def configkeyboard():
  togglekey = Keyboa(items=list([togglegroup,togglemember]), items_in_row=2).keyboard
  updatedbkey = Keyboa(items=[updatedb]).keyboard
  keyboard = Keyboa.combine(keyboards=(togglekey, updatedbkey, Keyboa(items=[cancel]).keyboard))
  return keyboard
def grouptogglepagekeyboardgenerator():
  grouplists = []
  grouplists.clear()
  with open(configdir, 'r') as configfile:
    config = json.load(configfile)
    for group in ['nogizaka46','sakurazaka46','hinatazaka46']:
      isservice = config['Sakamichi_App'][group]['telegram_services']['services']
      kanjiname = config['Sakamichi_App'][group]['telegram_services']['kanjiname']
      grouplists.append({
        "text": f"✓ {kanjiname}" if isservice else kanjiname,
        "callback_data": f"{kanjiname}toggle"
      })
  grouplistkey = Keyboa(items=list(grouplists),items_in_row=3).keyboard
  navkey       = Keyboa(items=list([backtoconfig,cancel]),items_in_row=2).keyboard
  keyboard     = Keyboa.combine(keyboards=(grouplistkey,navkey))
  return keyboard
def membertogglepagekeyboardgenerator():
  grouplists = []
  grouplists.clear()
  with open(configdir) as configfile:
    config = json.load(configfile)
    for group in ['nogizaka46','sakurazaka46','hinatazaka46']:
      isservice = config['Sakamichi_App'][group]['telegram_services']['services']
      kanjiname = config['Sakamichi_App'][group]['telegram_services']['kanjiname']
      if isservice:
        grouplists.append({
          "text": f"{kanjiname}",
          "callback_data":f"{kanjiname}enter"
          })
  grouplistkey = Keyboa(items=list(grouplists),items_in_row=3).keyboard
  navkey       = Keyboa(items=list([backtoconfig,cancel]),items_in_row=2).keyboard
  keyboard     = Keyboa.combine(keyboards=(grouplistkey,navkey))
  return keyboard
def membertogglepage2keyboardgenerator(group):
  memberlists = []
  memberlists.clear()
  with open(configdir) as configfile:
    config = json.load(configfile)
    for member in config['Sakamichi_App'][group]['telegram_services']['members']:
      memberid  = member['id']
      kanjiname = member['name'].replace(' ','')
      isservice = member['service']
      memberlists.append({
        "text": f"✓ {kanjiname}" if isservice else kanjiname,
        "callback_data": f"{group}-{memberid}"
      })
  memberlistkey = Keyboa(items=list(memberlists),items_in_row=3).keyboard
  navkey        = Keyboa(items=list([{"text":"Kembali","callback_data":"membertoggleservice"},cancel]),items_in_row=2).keyboard
  keyboard      = Keyboa.combine(keyboards=(memberlistkey, navkey))
  return keyboard

# MESSAGE HANDLER SECTION
@mobamebot.message_handler(commands=["start"])
def bot_start(message):
  if message.chat.type == 'private':
    mobamebot.send_message(message.chat.id, text=replystart.replace('%user%', str(message.from_user.username)), parse_mode='MarkdownV2')
  else:
    mobamebot.reply_to(message, text=replystart.replace(' %user%', ''), parse_mode='MarkdownV2')
  telemessagelogger(message)

@mobamebot.message_handler(commands=["tentang"])
def bot_about(message):
  if message.chat.type == 'private':
    mobamebot.send_message(message.chat.id, text=aboutmessage, parse_mode='MarkdownV2',disable_web_page_preview=True)
  else:
    mobamebot.reply_to(message, text=aboutmessage, parse_mode='MarkdownV2')
  telemessagelogger(message)

@mobamebot.message_handler(commands=["konfigurasi"])
def bot_config(message):
  if message.chat.id == ownerid:
    mobamebot.send_message(message.chat.id, text=configmessages, parse_mode="MarkdownV2", reply_markup=configkeyboard())
  else:
    msg = mobamebot.send_message(message.chat.id, text=preventintruders, parse_mode="MarkdownV2")
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    report = mobamebot.send_message(ownerid, text=report_message(message), parse_mode="MarkdownV2")
  telemessagelogger(message)

@mobamebot.message_handler(commands=["startsync"])
def startsynchronize(message):
  if message.chat.id == ownerid:
    global syncstate
    syncstate = True
    msg = mobamebot.send_message(message.chat.id, text=syncstarted)
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    logging.info(f"✨ Sinkronisasi dimulai pada {WIB.strftime('%Y-%m-%d %H:%M:%S')} WIB")
    logging.info(f"Interval sinkronisasi diset ke {SYNC_INTERVAL_SECONDS} detik")
    while syncstate:
      syncer()
      time.sleep(SYNC_INTERVAL_SECONDS)
    else:
      pass
  else:
    msg = mobamebot.send_message(message.chat.id, text=preventintruders, parse_mode="MarkdownV2")
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    report = mobamebot.send_message(ownerid, text=report_message(message), parse_mode="MarkdownV2")
  telemessagelogger(message)

@mobamebot.message_handler(commands=["stopsync"])
def stopsynchronize(message):
  if message.chat.id == ownerid:
    global syncstate
    syncstate = False
    logging.info(f"Sinkronisasi dihentikan pada {WIB.strftime('%Y-%m-%d %H:%M:%S')} WIB")
    msg = mobamebot.send_message(message.chat.id, text=syncstopped)
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
  else:
    msg = mobamebot.send_message(message.chat.id, text=preventintruders, parse_mode="MarkdownV2")
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    report = mobamebot.send_message(ownerid, text=report_message(message), parse_mode="MarkdownV2")
  telemessagelogger(message)

@mobamebot.message_handler(commands=['updatetoken'])
def update_token_func(message):
  if message.chat.id == ownerid:
    textsplit = message.text.split()
    if syncstate:
        msg = mobamebot.send_message(chat_id=message.chat.id, text="Harap mematikan sinkronisasi terlebih dahulu!")
        mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id - 1)
        time.sleep(3)
        mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
    if len(textsplit) == 3:
      grupname = textsplit[1]
      reftoken = textsplit[2]
      if grupname in sakamichigroups \
      and not syncstate\
      and len(reftoken.split('-')) == 5 \
      and len(reftoken.split('-')[0]) == 8 \
      and len(reftoken.split('-')[1]) == 4 \
      and len(reftoken.split('-')[2]) == 4 \
      and len(reftoken.split('-')[3]) == 4 \
      and len(reftoken.split('-')[4]) == 12:
        update_refresh_token(grupname, reftoken)
        msg = mobamebot.send_message(message.chat.id, text=refreshtokenupdated.replace('%grup%', grupname))
        mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id - 1)
        time.sleep(3)
        mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
      if grupname not in sakamichigroups:
        msg = mobamebot.send_message(chat_id=message.chat.id, text="Format nama grup yang anda masukkan salah!")
        mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id - 1)
        time.sleep(3)
        mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
      if len(reftoken.split('-')) != 5:
        msg = mobamebot.send_message(chat_id=message.chat.id, text="Format refresh token anda salah!")
        mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id - 1)
        time.sleep(3)
        mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
    if len(textsplit) != 3\
    and not syncstate:
        msg = mobamebot.send_message(chat_id=message.chat.id, text="Format command salah!")
        mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id - 1)
        time.sleep(3)
        mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
  else:
    msg = mobamebot.send_message(message.chat.id, text=preventintruders, parse_mode="MarkdownV2")
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    report = mobamebot.send_message(ownerid, text=report_message(message), parse_mode="MarkdownV2")
  telemessagelogger(message)

@mobamebot.message_handler(commands=['subsinfo'])
def get_sub_info(message):
  if message.chat.id in [ownerid, telechatid()]:
    textsplit = message.text.split()
    if len(textsplit) == 2\
    and textsplit[1] in sakamichigroups:
      mobamebot.send_message(message.chat.id, text=f"`{get_subsinfo(textsplit[1])}`", parse_mode="MarkdownV2")
    if len(textsplit) != 2\
    or textsplit[1] not in sakamichigroups:
      msg = mobamebot.send_message(message.chat.id,text=incorrectsubsinfo)
      mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id - 1)
      time.sleep(5)
      mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
  elif message.chat.id not in [ownerid, telechatid()]:
    msg = mobamebot.send_message(message.chat.id, text=preventintruders, parse_mode="MarkdownV2")
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    report = mobamebot.send_message(ownerid, text=report_message(message), parse_mode="MarkdownV2")
  telemessagelogger(message)
  
@mobamebot.message_handler(commands=['sendpastmessage'])
def send_pastmessages(message):
  if message.chat.id == ownerid:
    textsplit = message.text.split()
    if len(textsplit) == 3:
      config = configfile()
      if textsplit[1] == 'nogizaka46'\
      and textsplit[2] in [member['name'].replace(' ','') for member in config['Sakamichi_App'][textsplit[1]]['telegram_services']['members']]:
        main.Nogizaka.past_messages_streamer([textsplit[2]])
        message_sender_and_recorder(textsplit[1],main.nogipastmessages, nogilocalmsg)
      if textsplit[1] == 'sakurazaka46'\
      and textsplit[2] in [member['name'].replace(' ','') for member in config['Sakamichi_App'][textsplit[1]]['telegram_services']['members']]:
        main.Sakurazaka.past_messages_streamer([textsplit[2]])
        message_sender_and_recorder(textsplit[1],main.sakupastmessages, sakulocalmsg)
      if textsplit[1] == 'hinatazaka46'\
      and textsplit[2] in [member['name'].replace(' ','') for member in config['Sakamichi_App'][textsplit[1]]['telegram_services']['members']]:
        main.Hinatazaka.past_messages_streamer([textsplit[2]])
        message_sender_and_recorder(textsplit[1],main.hinapastmessages, hinalocalmsg)
      if textsplit[1] in sakamichigroups:
        msg = mobamebot.send_message(message.chat.id, text="Perintah diproses!")
        time.sleep(3)
        mobamebot.delete_message(msg.chat.id, msg.message_id)
      if textsplit[1] not in sakamichigroups\
      or textsplit[2] not in [member['name'].replace(' ','') for member in config['Sakamichi_App'][textsplit[1]]['telegram_services']['members']]\
      or syncstate:
        msg = mobamebot.send_message(message.chat.id, text="Maaf, nama grup yang anda masukkan salah! atau nama member yang anda masukkan tidak tersedia! Jangan lupa untuk mematikan sinkronisasi terlebih dahulu!")
        mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
        time.sleep(3)
        mobamebot.delete_message(msg.chat.id, msg.message_id)
    if len(textsplit) != 3:
      msg = mobamebot.send_message(message.chat.id,text=incorrectsendpast)
      mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id - 1)
      time.sleep(5)
      mobamebot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
  else:
    msg = mobamebot.send_message(message.chat.id, text=preventintruders, parse_mode="MarkdownV2")
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    report = mobamebot.send_message(ownerid, text=report_message(message), parse_mode="MarkdownV2")
  telemessagelogger(message)
@mobamebot.message_handler(commands=['ceksyncstate'])
def check_syncstate(message):
  if message.chat.id == ownerid:
    msg = mobamebot.send_message(message.chat.id, text=f"{'Sinkronisasi berjalan!' if syncstate else 'Sinkronisasi berhenti!'}")
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
  else:
    msg = mobamebot.send_message(message.chat.id, text=preventintruders, parse_mode="MarkdownV2")
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    report = mobamebot.send_message(ownerid, text=report_message(message), parse_mode="MarkdownV2")
  telemessagelogger(message)

@mobamebot.message_handler(commands=['autoscheduler'])
def autoscheduler_control(message):
  global auto_scheduler_enabled
  if message.chat.id != ownerid:
    msg = mobamebot.send_message(message.chat.id, text=preventintruders, parse_mode="MarkdownV2")
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    report = mobamebot.send_message(ownerid, text=report_message(message), parse_mode="MarkdownV2")
    telemessagelogger(message)
    return

  textsplit = message.text.split()
  if len(textsplit) == 1 or textsplit[1].lower() == 'status':
    status_text = f"Auto scheduler {'AKTIF' if auto_scheduler_enabled else 'NONAKTIF'}"
    msg = mobamebot.send_message(message.chat.id, text=status_text)
  elif textsplit[1].lower() == 'off':
    auto_scheduler_enabled = False
    logging.info("Auto scheduler dinonaktifkan manual oleh owner.")
    msg = mobamebot.send_message(message.chat.id, text="Auto scheduler dinonaktifkan. Sinkronisasi sekarang full manual.")
  elif textsplit[1].lower() == 'on':
    auto_scheduler_enabled = True
    logging.info("Auto scheduler diaktifkan manual oleh owner.")
    msg = mobamebot.send_message(message.chat.id, text="Auto scheduler diaktifkan.")
  else:
    msg = mobamebot.send_message(message.chat.id, text="Format: /autoscheduler [on|off|status]")

  mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
  time.sleep(3)
  mobamebot.delete_message(msg.chat.id, msg.message_id)
  telemessagelogger(message)

@mobamebot.message_handler(commands=['sendlog'])
def send_today_log(message):
  if message.chat.id == ownerid:
    msg = mobamebot.send_document(message.chat.id, document=open(f"{ROOT_DIR}{logdir}/logging.log", "rb"))
  else:
    msg = mobamebot.send_message(message.chat.id, text=preventintruders, parse_mode="MarkdownV2")
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    report = mobamebot.send_message(ownerid, text=report_message(message), parse_mode="MarkdownV2")
  telemessagelogger(message)
@mobamebot.message_handler()
def capture_unwanted(message):
  if message.chat.id not in [ownerid, botdebuggroup, botfinalgroup]:
    msg = mobamebot.send_message(message.chat.id, text=preventintruders, parse_mode="MarkdownV2")
    mobamebot.delete_message(msg.chat.id, msg.message_id - 1)
    time.sleep(3)
    mobamebot.delete_message(msg.chat.id, msg.message_id)
    report = mobamebot.send_message(ownerid, text=report_message(message), parse_mode="MarkdownV2")
    telemessagelogger(message)
 
# CALLBACK HANDLER SECTION
@mobamebot.callback_query_handler(func=lambda message: True)
def callback_option(call):
  if call.data == 'cancel':
    msg = mobamebot.delete_message(call.message.chat.id, call.message.message_id -1)
    mobamebot.delete_message(call.message.chat.id, call.message.message_id)
  if call.data == 'backtoconfig':
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=configmessages, parse_mode="MarkdownV2", reply_markup=configkeyboard())
  if call.data == 'updatedb':
    main.Nogizaka.teleservice_updater()
    main.Sakurazaka.teleservice_updater()
    main.Hinatazaka.teleservice_updater()
    mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text='Database Telah Diupdate', parse_mode="MarkdownV2")
    time.sleep(2)
    mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=configmessages, parse_mode="MarkdownV2", reply_markup=configkeyboard())
  if call.data == 'grouptoggleservice':
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=grouptogglepagekeyboardgenerator())
  elif call.data == 'membertoggleservice':
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=membertogglepagekeyboardgenerator())
  if call.data == '乃木坂46toggle':
    telegroupservicetoggler('nogizaka46')
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=grouptogglepagekeyboardgenerator())
  elif call.data == '櫻坂46toggle':
    telegroupservicetoggler('sakurazaka46')
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=grouptogglepagekeyboardgenerator())
  elif call.data == '日向坂46toggle':
    telegroupservicetoggler('hinatazaka46')
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=grouptogglepagekeyboardgenerator())
  if call.data == '乃木坂46enter':
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=membertogglepage2keyboardgenerator('nogizaka46'))
  elif call.data == '櫻坂46enter':
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=membertogglepage2keyboardgenerator('sakurazaka46'))
  elif call.data == '日向坂46enter':
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=membertogglepage2keyboardgenerator('hinatazaka46'))
  if "nogizaka46-" in call.data:
    telememberservicetoggler(call.data, 'nogizaka46')
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=membertogglepage2keyboardgenerator('nogizaka46'))
  elif "sakurazaka46-" in call.data:
    telememberservicetoggler(call.data, 'sakurazaka46')
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=membertogglepage2keyboardgenerator('sakurazaka46'))
  elif "hinatazaka46-" in call.data:
    telememberservicetoggler(call.data, 'hinatazaka46')
    msg = mobamebot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"{selectgm}", parse_mode="MarkdownV2", reply_markup=membertogglepage2keyboardgenerator('hinatazaka46'))
  
  telecalllogger(call)

if __name__ == "__main__":
  logging.info(f"✨ Bot started at {WIB}")
  while True:
      try:
          mobamebot.infinity_polling(timeout=50, long_polling_timeout=30)
      except KeyboardInterrupt:
          logging.info("Bot stopped manually.")
          break
      except requests.exceptions.ConnectionError as ce:
          logging.error(f"Koneksi terputus: {ce}. Mencoba kembali dalam 10 detik...")
          time.sleep(10)
      except Exception as e:
          logging.error(f"Error tidak terduga: {e}. Mencoba kembali dalam 5 detik...")
          time.sleep(5)
  
# AVAILABLE COMMAND
# start - Mulai Bot
# subsinfo - Informasi layanan Aktif
# tentang - Mengenai Bot dan Developer
# konfigurasi - (Khusus Owner) Aktifkan atau matikan layanan
# ceksyncstate - (Khusus Owner) Cek status sinkronisasi
# startsync - (Khusus Owner) Mulai sinkronisasi pesan
# stopsync - (Khusus Owner) Hentikan sinkronisasi pesan 
# updatetoken - (Khusus Owner) Update refresh token
# sendpastmessage - (Khusus Owner) Bagikan pesan member yang baru disubscribe 
# sendlog - (Khusus Owner) download file .log
