from google.appengine.ext import db

class PostDump(db.Model):
    handle = db.StringProperty(required=True)
    json = db.TextProperty()
    group = db.StringProperty()
    touch_time = db.DateTimeProperty(auto_now_add=True,
                                     auto_now=True)


