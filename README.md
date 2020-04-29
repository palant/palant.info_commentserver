This comment management server has been developed specifically for palant.info. It is unlikely to meet your exact requirements, provided here for reference only.

# Design goals

* Used by a website built on a static site generator (Hugo).
* Comments and replies stored in the <https://github.com/palant/palant.info> repository along with other website content.
* Pre-moderated comments, published only when approved by site owner.
* Direct replies only by site owner.
* Commenter's email address removed after moderation, notifications about replies only if provided during moderation.

# Requirements

[Python 3](https://www.python.org/) with [flask](http://flask.pocoo.org/), [markdown](https://python-markdown.github.io/), [bleach](https://bleach.readthedocs.io/), [frontmatter](https://python-frontmatter.readthedocs.io/) modules.

# Running

`server.py` is the WSGI application. It can be executed directly to start the test server on http://localhost:5000/.

The server expects a `config.ini` file in the same directory as `server.py`. Example configuration:

    [site]
    baseurl = https://palant.info
    publicdir = /var/www/public
    queuedir = /var/spool/comments

    [github]
    user = palant
    repository = palant.info
    access_token = 1234567890abcdef1234567890abcdef12345678

    [mail]
    smtp_server = mail.example.com
    sender = me@example.com

The `access_token` value must be a GitHub access token with write privileges for the GitHub repository.

# Comment submission process

The comment submission form can be seen in `layout/_default/single.html` template in the <https://github.com/palant/palant.info> repository and the JavaScript code behind it is in `static/js/comments.js`. The server receives the comment, validates it and saves it to the queue directory with a random name. The blog owner is notified with an email mentioning the URL where the comment can be reviewed.

The blog post is identified by its URI in the path. To validate the URI and retrieve additional data, the server reads the static file generated for the blog post from the server's public directory. In particular, it expects to find a `data-path` attribute on the comment form determining the path of the blog post within the original repository.

The blog owner can either approve or reject the comment in the moderation interface, optionally specifying a reply. If the comment is approved, both the comment and the reply are added to the GitHub repository. With either action the comment data is removed from the queue, and with it the commenter's email address.

# Security considerations

To prevent CSRF attacks, comment submission is required to send `X-XMLHttpRequest` header. This ensures that it cannot be triggered by third party websits.

Comment moderation interface is only accessible with knowledge of the name under which the comment has been stored in the queue. With the name being based on 16 random bytes, bruteforcing it remotely is unrealistic.

Ideally, comments would be stored in the repository as entered. However, Hugo cannot currently sanitize untrusted Markdown content. So comments will be converted to HTML and sanitized when received by this server.

Blog owner replies will also be sanitized. This protects against the unlikely event that an attacker can gain access to comment moderation interface.
