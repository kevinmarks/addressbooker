# Copyright (C) 2008 Brad Fitzpatrick
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


__author__ = 'brad@danga.com (Brad Fitzpatrick)'

# Core Python
import cgi
import logging
import pprint
import random
import re
import urllib

# Core/AppEngine stuff
import wsgiref.handlers
from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.ext import db
from google.appengine.ext.webapp import template
from google.appengine.api import urlfetch

# Libraries included w/ app
import atom
import atom.http_interface
import atom.token_store
import atom.url
import gdata.alt.appengine
import gdata.auth
import gdata.contacts.service as contactsservice
import gdata.service
import simplejson

# App stuff
import settings
import models

VALID_HANDLE = re.compile(r"^\w+$")


def NumberSuffixesMatch(num1, num2):
  """Given two phone numbers, return bool if they match. 

  Numbers are strings.  A match is 7 matching final
  numbers, ignoring punctuations and space and stuff.
  """
  num1 = re.sub(r"[^\d]", "", num1)
  num2 = re.sub(r"[^\d]", "", num2)
  if len(num1) < 6 or len(num2) < 6:
    return False
  return num1[-7:] == num2[-7:]


def PhoneNumberListContainsNumber(number_list, number):
  """Searches number_list for number.  Returns True if found.

  Args:
    number_list: list of gdata PhoneNumber
    number: string phone number.
    
  Returns: bool.
  """
  for phone_number in number_list:
    google_number = phone_number.text
    if NumberSuffixesMatch(google_number, number):
      return True
  return False


def GroupListContainsGroup(group_list, group):
  """Returns true if group found in group_list.

  Args:
    group_list: array of GroupMembershipInfo
    group: a GroupMembershipInfo
    
  Returns: bool.
  """
  assert group

  sought_href = group.href
  for potential_match in group_list:
    if potential_match.href == sought_href:
      return True
  return False


def FindEntryToMergeInto(contact, feed):
  """Finds Entry (or None) in feed to merge contact into."""
  contact_name = contact["name"]
  for entry in feed.entry:
    if entry.title and entry.title.text and \
       entry.title.text == contact_name:
      return entry
      
    for number_rec in contact["numbers"]:
      contact_number = number_rec["number"]
      if PhoneNumberListContainsNumber(entry.phone_number,
                                       contact_number):
        return entry

  return None


def PhoneRelType(text):
  """Given some free-formish text, map that to the GData phone rel."""
  if re.match(r"^\s*(mobile|cell)", text, re.I):
    return "http://schemas.google.com/g/2005#mobile"
  if re.match(r"^\s*(work|office)", text, re.I):
    return "http://schemas.google.com/g/2005#work"
  if re.match(r"^\s*(house|home)", text, re.I):
    return "http://schemas.google.com/g/2005#home"
  return "http://schemas.google.com/g/2005#other"


def NewContactEntry(contact, group=None):
  """Make a new GData Contact Entry from a submitted contact dict.

  Returns: The new, populated cgdata.contacts.ContactEntry object.
  """
  new_entry = gdata.contacts.ContactEntry()
  UpdateContactEntry(new_entry, contact, group)
  return new_entry


def UpdateContactEntry(merge_entry, contact, group=None):
  """Merge user-submitted contact data into GData entry.

  Args:
    merge_entry: The gdata.contacts.ContactEntry() object to
      merge into:
    contact: Input contact dictionary from user w/ fields
    group: optional GroupMembershipInfo to put user in

  Returns: List of changes (terse English each).  If no changes,
    returns the empty list, which is false in boolean context.
  """
  changes = []

  if contact["name"]:
    # TODO: promote short names (e.g. "Brad" or "Brad F.") to
    # full names ("Brad Fitzpatrick").  For now, never modify
    # the name.
    if not merge_entry.title or not merge_entry.title.text:
      changes.append("set name: %s" % contact["name"])
      merge_entry.title = atom.Title(text=contact["name"])

  for number_rec in contact["numbers"]:
    if not PhoneNumberListContainsNumber(merge_entry.phone_number,
                                         number_rec["number"]):
      changes.append("adding number: %s" % number_rec["number"])
      merge_entry.phone_number.append(gdata.contacts.PhoneNumber(
          rel=PhoneRelType(number_rec["type"]),
          text=number_rec["number"]))

  if group and not GroupListContainsGroup(merge_entry.group_membership_info,
                                          group):
    changes.append("adding to group.")
    merge_entry.group_membership_info.append(group)

  return changes
  

class Updater(object):
  """Queues up updates and flushes them to gdata batch as needed."""

  def __init__(self, client=None):
    self.client = client
    self.batch_feed = gdata.contacts.ContactsFeed()

  def AddInsert(self, entry):
    self.batch_feed.AddInsert(entry)
    self.FlushIfNeeded()

  def AddUpdate(self, entry):
    self.batch_feed.AddUpdate(entry)
    self.FlushIfNeeded()

  def FlushIfNeeded(self):
    if len(self.batch_feed.entry) >= 50:   # could be 100 max
      self.Flush()

  def Flush(self):
    if not len(self.batch_feed.entry):
      return
    self.client.ExecuteBatch(self.batch_feed,
                             gdata.contacts.service.DEFAULT_BATCH_URL)
    self.batch_feed = gdata.contacts.ContactsFeed()


class AddressBooker(webapp.RequestHandler):

  def get(self):
    self.response.headers['Content-Type'] = 'text/html'
    
    self.response.out.write("""<!DOCTYPE html><html><head>
         <title>AddressBooker: merge contacts into your Google Address Book
         </title>
         <link rel="stylesheet" type="text/css" 
               href="/static/feedfetcher.css"/>
         </head><body>""")
       
    self.response.out.write("""<div id="nav"><a href="/">Home</a>""")
    if users.get_current_user():
      self.response.out.write('<a href="%s">Sign Out</a>' % (
          users.create_logout_url('http://%s/merge/' % settings.HOST_NAME)))
    else:
      self.response.out.write('<a href="%s">Sign In</a>' % (
          users.create_login_url('http://%s/merge/' % settings.HOST_NAME)))
    self.response.out.write('</div>')


  def post(self):
    handle = self.request.get('handle')
    if not handle:
      raise "Missing argument 'handle'"
    if not VALID_HANDLE.match(handle):
      raise "Bogus handle."

    json = self.request.get('json')
    group = self.request.get('group')
    
    if handle:
      post_dump = models.PostDump(key_name="handle:" + handle,
                                  json=json,
                                  group=group,
                                  handle=handle)
      post_dump.put()
      
    contacts = simplejson.loads(json)

    self.response.out.write(template.render('now_what.html', {
      'n_contacts': len(contacts),
      'handle': str(post_dump.key()),
    }))
    
    #self.response.out.write("You posted: " + pprint.pformat(contacts));


class MergeView(webapp.RequestHandler):
  """View the contacts for a given handle."""

  def get(self):
    key = self.request.get('key')
    if not key:
      raise "Missing argument 'key'"
    post_dump = models.PostDump.get(db.Key(key))
    if not post_dump:
      raise "State lost?  Um, do it again."

    contacts = simplejson.loads(post_dump.json)
    for contact in contacts:
      self.response.out.write("<br clear='both'><h2>%s</h2>" % contact["name"])
      self.response.out.write("<img src='%s' style='float:left' />" % contact["img"])
      for number in contact["numbers"]:
        obf_number = re.sub(r"\d{3}$", "<i>xxx</i>", number["number"])
        self.response.out.write("<p><b>%s</b> %s</p>" % (
          number["type"], obf_number))


class MergeGoogle(webapp.RequestHandler):
  """Merge contacts into Google Contacts w/ Google Contacts API."""

  def get(self):
    key = self.request.get('key')
    if not key:
      raise "Missing argument 'key'"
    post_dump = models.PostDump.get(db.Key(key))
    if not post_dump:
      raise "State lost?  Um, do it again."

    def out(str):
      self.response.out.write(str)

    user = users.get_current_user()
    logging.info("Current user: " + str(user))

    # We need a logged-in user for the GData.client.token_store to work.
    if not user:
      logging.info("Redirecting to sign-in.");
      sign_in_url = users.create_login_url('http://%s/merge/google?key=%s' %
                                           (settings.HOST_NAME, key))
      self.redirect(sign_in_url)
      return

    # And the subclass of the Service for the Contacts API:
    client = contactsservice.ContactsService()
    gdata.alt.appengine.run_on_appengine(client)

    contacts = simplejson.loads(post_dump.json)

    contacts_url = "http://www.google.com/m8/feeds/contacts/default/full"
    auth_base_url = "http://www.google.com/m8/feeds/"

    session_token = client.token_store.find_token(auth_base_url)
    if type(session_token) == atom.http_interface.GenericToken:
      session_token = None

    if not session_token:
      # Find the AuthSub token and upgrade it to a session token.
      auth_token = gdata.auth.extract_auth_sub_token_from_url(self.request.uri)
      if auth_token:
        session_token = client.upgrade_to_session_token(auth_token)
        client.token_store.add_token(session_token)
        # just to sanitize our URL:
        self.redirect('http://%s/merge/google?key=%s' %
                      (settings.HOST_NAME, key))
      else:
        next = self.request.uri
        auth_sub_url = client.GenerateAuthSubURL(next, auth_base_url,
                                                 secure=False, session=True)
        self.redirect(str(auth_sub_url))
      return

    out("<p>We're good to go.  %s</p>" % str(session_token));
    sign_out_url = users.create_logout_url('http://%s/merge/' %
                                          (settings.HOST_NAME))
    out("\nOr you want to <a href='%s'>log out</a>?" % sign_out_url)

    # Process Groups
    groups_feed = client.Get("http://www.google.com/m8/feeds/groups/default/full")
    groups_feed = gdata.contacts.GroupsFeedFromString(str(groups_feed))  # TODO: unicode safety
    #  TODO: something like: groups_feed.ToString().decode("utf-8") ??
    group_name = {}  # id -> name
    group_id = {}    # name -> id
    for group in groups_feed.entry:
      group_name[group.id.text] = group.content.text
      group_id[group.content.text] = group.id.text
      if True:
        out("<h3>Group</h3><ul>")
        out("<li>id: %s</li>" % cgi.escape(group.id.text))
        out("<li>content: %s</li>" % cgi.escape(group.content.text))
        out("</ul>")

    dest_group_name = post_dump.group or "AddressBooker"
    if dest_group_name not in group_id:
      new_group = gdata.contacts.GroupEntry(title=atom.Title(
          text=dest_group_name))
      group = client.CreateGroup(new_group)
      out("<h3>group: %s</h3>" % group.id)
      group_name[group.id] = dest_group_name
      group_id[dest_group_name] = group.id

    full_feed_url = contacts_url + "?max-results=99999"
    feed = client.Get(full_feed_url, converter=gdata.contacts.ContactsFeedFromString)

    updater = Updater(client=client);

    group = gdata.contacts.GroupMembershipInfo(href=unicode(group_id[dest_group_name]))

    for contact in contacts:
      out("<br clear='both'><h2>%s</h2>" % contact["name"])
      out("<img src='%s' style='float:left' />" % contact["img"])
      for number in contact["numbers"]:
        out("<p><b>%s</b> %s</p>" % (number["type"], number["number"]))
      merge_entry = FindEntryToMergeInto(contact, feed)
      if merge_entry:
        changes = UpdateContactEntry(merge_entry, contact, group=group)
        if changes:
          out("<p><b>Action: merge</b> into existing Google contact, <i>%s</i></p>" % cgi.escape(
              merge_entry.title.text.decode("utf-8")))
          out("<p><b>Applying changes: </b><ul>")
          for c in changes:
            out("<li>%s</li>" % cgi.escape(c))
          out("</ul>")
          updater.AddUpdate(merge_entry)
        else:
          out("<p><b>Action: no changes</b>, record already up-to-date.</p>")
      else:
        out("<p><b>Action: new Google Contact</b>")
        updater.AddInsert(NewContactEntry(contact, group=group))

    render_google_list = False
    if render_google_list:
      out("<hr />")
      for entry in feed.entry:
        if entry.title and entry.title.text:
          out('<h3>Entry Title: %s</h3>' % (
              entry.title.text.decode('UTF-8')))
        else:
          out("<h3>(title-less entry)</h3>");
      
        for phone_number in entry.phone_number:
          out("<p><b>Phone: (%s)</b> %s</p>" %
                                (phone_number.rel, phone_number.text))

        for email in entry.email:
          out("<p><b>Email: (%s)</b> %s</p>" %
                                (email.rel, email.address))

        for group in entry.group_membership_info:
          out("<p><b>Group: (%s)</b> %s</p>" %
              (group.href, cgi.escape(str(group))))

    updater.Flush()

    

class Acker(webapp.RequestHandler):
  """Simulates an HTML page to prove ownership of this domain for AuthSub 
  registration."""

  def get(self):
    self.response.headers['Content-Type'] = 'text/plain'
    self.response.out.write('This file present for AuthSub registration.')


def main():
  application = webapp.WSGIApplication([
    ('/merge/', AddressBooker),
    ('/merge/google', MergeGoogle), 
    ('/merge/view', MergeView), 
    ('/google72db3d6838b4c438.html', Acker),
    ], debug=True)
  wsgiref.handlers.CGIHandler().run(application)


if __name__ == '__main__':
  main()
