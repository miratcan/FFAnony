#!/usr/bin/env python

from os.path import join, dirname

from google.appengine.ext import db
from google.appengine.api import urlfetch
from google.appengine.api import images
from google.appengine.api import users

from django.utils import simplejson as json

import webapp2

from webapp2_extras import jinja2

import jinja2
import webapp2
import cgi
import base64
import urllib
import logging

import config

TEMPLATES_DIR = join(dirname(__file__), "templates")
JINJA_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES_DIR))
AUTHSTR = "Basic " + base64.b64encode('%s:%s' % (config.USERNAME, config.REMOTE_KEY))
MAX_ENTRY_LENGTH = 350
ENTRY_NSFW_PREFIX = "[ #nsfw ] "

## Models #####################################################################
class Entry(db.Model):
    """
    Holds entries to post friendfeed"""

    body = db.TextProperty()
    eid = db.StringProperty()
    url = db.LinkProperty()
    nsfw = db.BooleanProperty(default=False)
    status = db.StringProperty(required=True, default="draft",
        choices={"draft", "pending", "accepted", "rejected", "deleted", "published"}
    )
    date = db.DateTimeProperty(auto_now_add=True)


class Attachment(db.Model):
    """
    Holds attachments to append Entries"""

    entry = db.ReferenceProperty(Entry)
    image = db.BlobProperty()


#- Handlers -------------------------------------------------------------------
class MainView(webapp2.RequestHandler):
    def get(self):
        self.response.headers['Content-Type'] = 'text/html'
        self.response.out.write(JINJA_ENV.get_template('form.html').render())

    def post(self):

        # if there is no body, return with error message
        if not self.request.get('body'):
            self.response.out.write(
                JINJA_ENV.get_template('form.html').render({
                    "err_msg": "Empty message?"}))
            return

        # TODO: check uploaded files are images, if not: return error message

        self.response.headers['Content-Type'] = 'text/html'
        entry = Entry(
            body=cgi.escape(self.request.get('body')),
            nsfw=bool(self.request.get("nsfw", default_value=False))
        )
        entry.put()

        if self.request.get_all("img"):
            for image_data in self.request.get_all("img"):
                if image_data:
                    attachment = Attachment(
                        entry=entry,
                        image=db.Blob(str(image_data)))
                    attachment.put()

        self.redirect("/entry/?key=" + str(entry.key()))


class EntryView(webapp2.RequestHandler):
    def get(self):
        # Try to get entry
        entry = Entry.get(self.request.get("key"))

        # If entry is not found, show error message.
        if not entry:
            self.response.out.write("Not found.")
            return

        # Try to get action.
        action = self.request.get("action")

        if action:
            if action == "delete":

                payload = urllib.urlencode({"id": entry.eid})

                # Make delete request to friendfeed.
                result = urlfetch.fetch(
                    url="http://friendfeed.com/api/v2/entry/delete",
                    method=urlfetch.POST,
                    payload=payload,
                    headers={'Authorization': AUTHSTR})

                # If success, mark entry as deleted.
                if int(result.status_code) == 200:
                    entry.status = "deleted"
                    entry.put()
                else:
                    logging.error(result)

            # If action is public, mark it as pending for admin review.
            if action == "publish" and entry.status == "draft":
                entry.status = "pending"
                entry.put()

        self.response.out.write(JINJA_ENV.get_template('entry.html').render({
            "entry": entry}))


class AttachmentView(webapp2.RequestHandler):
    def get(self):
        attachment = Attachment.get(self.request.get("key"))
        if attachment:

            try:
                related_entry = attachment.entry
            except db.ReferencePropertyResolveError:
                related_entry = None

            if not related_entry or related_entry.status == "deleted":
                self.redirect('/static/no-image.jpg')

            if self.request.get("thumbnail"):
                image_data = images.resize(attachment.image, 100, 80)
            else:
                image_data = attachment.image
            self.response.headers['Content-Type'] = 'image/jpeg'
            self.response.out.write(image_data)
        else:
            self.redirect('/static/no-image.jpg')


class AdminView(webapp2.RequestHandler):

    def get(self):

        user = users.get_current_user()

        if user and users.is_current_user_admin():
            filter_by = self.request.get("filter_by")
            if filter_by and filter_by in ("pending", "accepted", "rejected",
                                           "published", "deleted", "draft"):
                query = db.GqlQuery("SELECT * FROM Entry WHERE "
                                    "status='%s' ORDER BY date "
                                    "DESC" % filter_by)
            else:
                query = db.GqlQuery("SELECT * FROM Entry ORDER BY date DESC")
            entries = query.fetch(50)
            self.response.out.write(
                JINJA_ENV.get_template('admin.html').render({
                    "entries": entries,
                    "filter_by": filter_by
                }))
        else:
            self.response.out.write(\
                "May be you have to <a href='%s'>login</a>" % \
                    users.create_login_url("/admin/"))

    def post(self):
        """

        """
        action = self.request.get("action")
        if action:
            if action in ("accepted", "rejected", "deleted"):
                for entry_key in self.request.get_all("entry_key"):
                    entry = Entry.get(entry_key)
                    entry.status = action
                    entry.put()
            elif action in ("nsfw", "sfw"):
                for entry_key in self.request.get_all("entry_key"):
                    entry = Entry.get(entry_key)
                    entry.nsfw = action == "nsfw"
                    entry.put()

        self.redirect("/admin/")


class PushAccepted(webapp2.RequestHandler):
    def get(self):

        # Fetch, accepted entries

        entries = db.GqlQuery(
            "SELECT * FROM Entry WHERE status='accepted'"
            "ORDER BY date ASC").fetch(1)

        if entries:
            for entry in entries:
                max_entry_length = MAX_ENTRY_LENGTH

                # build urls of image attachments of entry as list
                image_urls = [
                    "http://ffanony.appspot.com/attachment/?key=" + \
                    str(attachment.key()) for attachment in \
                            entry.attachment_set.fetch(3)]

                if entry.nsfw:
                    data_dict = {"body": ENTRY_NSFW_PREFIX}
                    max_entry_length -= len(ENTRY_NSFW_PREFIX)
                else:
                    data_dict = {"body": ""}

                # if body text is longer than max_entry_length, split it two
                # parts as body, and comment. So people can make long posts
                if len(entry.body) > max_entry_length:
                    data_dict["body"] += entry.body.encode("utf-8")[:max_entry_length - 3] + "..."
                    data_dict["comment"] = "..." + entry.body.encode("utf-8")[MAX_ENTRY_LENGTH - 6:]
                else:
                    data_dict["body"] += entry.body.encode("utf-8")


                if entry.nsfw and image_urls:
                    if data_dict.has_key("comment"):
                        print data_dict["comment"]
                        data_dict["comment"] += "( %s )" % (" ".join(image_urls),)
                    else:
                        data_dict["comment"] = " ( %s )" % (" ".join(image_urls),)
                else:
                    data_dict["image_url"] = ",".join(image_urls)

               # Convert data_dict to a urlencoded string
                payload = urllib.urlencode(data_dict, "utf-8")

                # Go johny go http://www.youtube.com/watch?v=uMY5VGYh2Go
                result = urlfetch.fetch(
                    url="http://friendfeed.com/api/v2/entry",
                    method=urlfetch.POST,
                    payload=payload,
                    headers={'Authorization': AUTHSTR}
                )

                # If gone, Save eid and url of post.
                if int(result.status_code) == 200:
                    result_data = json.loads(result.content)
                    entry.eid = result_data['id']
                    entry.url = result_data['url']
                    entry.status = "published"
                    entry.put()
                else:
                    logging.error(result)
                self.response.out.write("pushed: %s\n" % entry.body)
        else:
            self.response.out.write("Nothing to push.")

logging.getLogger().setLevel(logging.DEBUG)

app = webapp2.WSGIApplication([
    ('/', MainView),
    ('/entry/', EntryView),
    ('/attachment/', AttachmentView),
    ('/admin/', AdminView),
    ('/push/', PushAccepted),
])
