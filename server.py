#!/usr/bin/env python3

import base64
import configparser
import datetime
import email
import html
import json
import os
import re
import secrets
import smtplib
import subprocess
import sys
import time
import traceback
import urllib

import bs4
import flask
import frontmatter
import jinja2
import mf2py

from format import format_comment, cleaner_stripping as cleaner

basedir = os.path.dirname(sys.argv[0]) or '.'
config = configparser.ConfigParser()
config.read(os.path.join(basedir, 'config.ini'))

debug = False
app = flask.Flask(
    'comment_management',
    template_folder=os.path.join(basedir, 'templates')
)

def add_debug_header(name, value):
    def decorator(func):
        def wrapper(*args):
            response = func(*args)
            if debug:
                response.headers[name] = value
            return response
        return wrapper
    return decorator


def resolve_path(path):
    if not os.path.isabs(path):
        path = os.path.join(basedir, path)
    return os.path.abspath(path)


def is_same_origin(url1, url2):
    try:
        parsed1 = urllib.parse.urlparse(url1)
        parsed2 = urllib.parse.urlparse(url2)
    except:
        return False
    return parsed1.scheme == parsed2.scheme and parsed1.netloc == parsed2.netloc


def get_article_path(uri):
    dir = resolve_path(config.get('site', 'publicdir'))
    path = os.path.abspath(os.path.join(dir, *uri.strip('/').split('/'), 'index.html'))
    if os.path.commonprefix([dir, path]) != dir:
        return None, None

    try:
        with open(path, 'r', encoding="utf-8") as file:
            contents = file.read()
    except:
        return None, None

    path_match = re.search(r'<form\b[^>]*data-path="(.*?)"', contents)
    if not path_match:
        return None, None

    title_match = re.search(r'<title>(.*?)</title>', contents)

    return (
        html.unescape(path_match.group(1)),
        html.unescape(title_match.group(1))
    )


def get_queue_dir():
    return resolve_path(config.get('site', 'queuedir'))


def github_request(method, uri, data=None):
    headers = {
        'Authorization': 'token {}'.format(config.get('github', 'access_token')),
        'User-Agent': 'Blog comment management (user {})'.format(config.get('github', 'user'))
    }
    if data:
        headers['Content-Type'] = 'application/json; charset=utf-8'
        data = json.dumps(data).encode('utf-8')

    req = urllib.request.Request('https://api.github.com/repos/{}/{}/{}'.format(
        config.get('github', 'user'),
        config.get('github', 'repository'),
        uri
    ), headers=headers, method=method)

    with urllib.request.urlopen(req, data) as response:
        return json.load(response)


def save_comment(comment_data, reply):
    old_main = github_request('GET', 'commits/main')

    dirpath = comment_data['article'].strip('/')
    dir_contents = github_request('GET', 'contents/content/{}'.format(dirpath))

    index_path = None
    index_url = None
    maxcomment = 0
    for entry in dir_contents:
        if entry.get('type') != 'file':
            continue
        if entry['name'].startswith('index.'):
            index_path = flask.safe_join('content', *dirpath.split('/'), entry['name'])
            index_url = entry['download_url']
        match = re.search(r'^comment_(\d+)\.', entry['name'])
        if match:
            id = int(match.group(1))
            if id > maxcomment:
                maxcomment = id

    tree = []
    comment_id = '{:06}'.format(maxcomment + 1)
    comment_contents = json.dumps({
        'publishDate': comment_data['date'],
        'author': comment_data['name'],
        'authorUrl': comment_data['web'],
        'type': comment_data.get('type', 'comment'),
        'title': comment_data.get('mentionTitle', ''),
        'id': comment_id,
    }, indent=2) + '\n\n' + comment_data['message']
    tree.append({
        'path': flask.safe_join('content', *dirpath.split('/'), 'comment_{}.html'.format(comment_id)),
        'mode': '100644',
        'type': 'blob',
        'content': comment_contents,
    })

    if reply:
        reply_id = '{:06}'.format(1)
        reply_contents = json.dumps({
            'id': reply_id,
            'publishDate': datetime.datetime.utcnow().isoformat(' ', 'seconds')
        }, indent=2) + '\n\n' + reply
        tree.append({
            'path': flask.safe_join('content', *dirpath.split('/'), 'comment_{}_reply_{}.html'.format(comment_id, reply_id)),
            'mode': '100644',
            'type': 'blob',
            'content': reply_contents,
        })

    with urllib.request.urlopen(index_url) as file:
        post = frontmatter.loads(file.read().decode('utf-8'))
    post.metadata['lastmod'] = datetime.datetime.utcnow().isoformat(' ', 'seconds')
    tree.append({
        'path': index_path,
        'mode': '100644',
        'type': 'blob',
        'content': frontmatter.dumps(post),
    })

    tree = github_request('POST', 'git/trees', {
        'base_tree': old_main['commit']['tree']['sha'],
        'tree': tree
    })['sha']

    commit = github_request('POST', 'git/commits', {
        'message': 'Added blog comment',
        'tree': tree,
        'parents': [old_main['sha']],
    })['sha']

    github_request('PATCH', 'git/refs/heads/main', {
        'sha': commit
    })

    return comment_id


def formatmime(text):
    # See http://bugs.python.org/issue5871 (not really fixed), Header() will
    # happily accept non-printable characters including newlines. Make sure to
    # remove them.
    text = re.sub(r'[\x00-\x1F]', '', text)
    return email.header.Header(text).encode()


def send_mail(template_name, from_addr, to_addr, **params):
    params['sender'] = from_addr
    params['recipient'] = to_addr
    params['baseurl'] = config.get('site', 'baseurl')

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(app.template_folder),
        autoescape=True
    )
    env.filters['mime'] = formatmime

    message = env.get_template(template_name).render(params)
    with smtplib.SMTP(config.get('mail', 'smtp_server')) as smtp:
        smtp.sendmail(from_addr, to_addr, message.encode('utf-8'))


def trim_html(html, ideal_length, max_length):
    if len(html) > ideal_length:
        index1 = html.rfind('<', 0, ideal_length)
        index2 = html.rfind('>', 0, ideal_length)
        if index1 >= 0 and index2 < index1:
            html = html[0:index1]
        else:
            match = re.search(r'[.?!<]', html[ideal_length:])
            if match:
                html = html[0:ideal_length + match.start()]
        if len(html) > max_length:
            html = html[0:max_length]
        html = html + '…'
    return html


def validate_mention(data):
    with urllib.request.urlopen(data['source'], timeout=10) as response:
        content_type = response.info().get('Content-Type', '').lower()
        if content_type != 'text/html' and not content_type.startswith('text/html;'):
            raise Exception('Unexpected content type: {}'.format(content_type))

        contents = response.read(1024 * 1024)

    doc = bs4.BeautifulSoup(contents, 'html5lib')
    valid = False
    entry = None
    expected = config.get('site', 'baseurl') + data['uri']
    for link in doc.find_all('a'):
        if link.get('href') == expected:
            valid = True
            entry = link.find_parent(class_='h-entry')
            if entry:
                break

    if not valid:
        raise Exception('Link not found on the source page')

    if entry:
        props = mf2py.parse(entry, url=data['source'])['items'][0]['properties']

        if props.get('url'):
            data['web'] = props.get('url')[0]
        if props.get('name'):
            data['mentionTitle'] = props.get('name')[0]

        if props.get('content'):
            message = props['content'][0]['html']
        else:
            message = str(entry)
        data['message'] = cleaner.clean(trim_html(message, 2000, 2500))

        authors = set()
        for author in props.get('author', []):
            for name in author['properties'].get('name', []):
                authors.add(name)
        data['name'] = ', '.join(sorted(authors))

    if not data.get('web'):
        el = doc.find('link', rel='canonical')
        if el:
            data['web'] = el.get('href', '').strip()

    for selector in [{'name': 'meta', 'property': 'og:title'}, {'name': 'title'}]:
        if not data.get('mentionTitle'):
            el = doc.find(**selector)
            if el:
                data['mentionTitle'] = el.get('content', el.get_text()).strip()

    for selector in [{'name': 'meta', 'attrs': {'name': 'description'}}, {'name': 'meta', 'property': 'og:description'}]:
        if not data.get('message'):
            el = doc.find(**selector)
            if el:
                data['message'] = cleaner.clean(trim_html(el.get('content', '').strip(), 2000, 2500))

    if not data.get('name'):
        el = doc.find('meta', attrs={'name': 'author'})
        if el:
            data['name'] = el.get('content', '').strip()

    if not data.get('web') or not is_same_origin(data['web'], data['source']):
        data['web'] = data['source']

    if data.get('message', '').endswith('…'):
        data['message'] = data['message'] + ' <a href="{}">more</a>'.format(data['web'])


@app.route('/comment/submit', methods=['POST', 'OPTIONS'])
@add_debug_header('Access-Control-Allow-Origin', 'http://localhost:1313')
@add_debug_header('Access-Control-Allow-Headers', 'X-XMLHttpRequest')
def submit_comment():
    if flask.request.method == 'OPTIONS':
        return flask.make_response('', 200)

    if 'X-XMLHttpRequest' not in flask.request.headers:
        return flask.jsonify({'error': True, 'message': 'X-XMLHttpRequest header missing from request.'})

    name = flask.request.form.get('name', '').strip()
    if not name:
        return flask.jsonify({'error': True, 'message': 'Name is mandatory.'})

    email = flask.request.form.get('email', '').strip()
    if email and (not '@' in email or re.search(r'\s', email)):
        return flask.jsonify({'error': True, 'message': 'Invalid email.'})

    web = flask.request.form.get('web', '').strip()
    if web and not re.match(r'^https?://\S+$', web):
        return flask.jsonify({'error': True, 'message': 'Invalid website.'})

    message = flask.request.form.get('message', '').strip()
    if not message:
        return flask.jsonify({'error': True, 'message': 'Comment message is mandatory.'})
    message_html = format_comment(message)

    uri = flask.request.form.get('uri', '').strip()
    if not uri or not uri.startswith('/') or re.search(r'\s', uri):
        return flask.jsonify({'error': True, 'message': 'Article URI not specified or invalid.'})

    article, title = get_article_path(uri)
    if not article:
        return flask.jsonify({'error': True, 'message': 'Could not find article path.'})

    id = secrets.token_hex(32)
    data = {
        'id': id,
        'date': datetime.datetime.utcnow().isoformat(' ', 'seconds'),
        'name': name,
        'email': email,
        'web': web,
        'message': message_html,
        'uri': uri,
        'article': article,
        'title': title,
    }

    with open(os.path.join(get_queue_dir(), id), 'w', encoding='utf-8') as file:
        json.dump(data, file, ensure_ascii=False)

    sender = config.get('mail', 'sender')
    send_mail('new_comment.mail', sender, sender, **data)
    return flask.jsonify({'error': False, 'message': 'Your comment has been submitted and awaits moderation.'})


@app.route('/mention/submit', methods=['POST', 'OPTIONS'])
def submit_mention():
    if flask.request.method == 'OPTIONS':
        return flask.make_response('', 200)

    source = flask.request.form.get('source', '').strip()
    target = flask.request.form.get('target', '').strip()
    if not source or not target:
        return flask.make_response('Source and target are mandatory.', 400)

    try:
        scheme = urllib.parse.urlparse(source).scheme
        if scheme not in ('http', 'https'):
            raise Exception()
    except:
        return flask.make_response('Failed to parse source URL.', 400)

    try:
        uri = urllib.parse.urlparse(target).path
    except:
        return flask.make_response('Failed to parse target URL.', 400)
    if not uri or not uri.startswith('/') or re.search(r'\s', uri):
        return flask.make_response('Article URI not specified or invalid.', 400)

    article, title = get_article_path(uri)
    if not article:
        return flask.make_response('Could not find article path.', 400)

    id = secrets.token_hex(32)
    data = {
        'id': id,
        'date': datetime.datetime.utcnow().isoformat(' ', 'seconds'),
        'type': 'mention',
        'source': source,
        'uri': uri,
        'article': article,
        'title': title,
    }

    with open(os.path.join(get_queue_dir(), id), 'w', encoding='utf-8') as file:
        json.dump(data, file, ensure_ascii=False)

    sender = config.get('mail', 'sender')
    send_mail('new_mention.mail', sender, sender, **data)
    return flask.make_response('', 202)


@app.route('/comment/review/<id>', methods=['GET', 'POST'])
def review_comment(id):
    if not re.match(r'^[\da-f]+$', id):
        return flask.make_response('Invalid ID', 500)

    path = os.path.join(get_queue_dir(), id)
    with open(path, 'r', encoding='utf-8') as file:
        data = json.load(file)

    if flask.request.method == 'GET':
        if data.get('type') == 'mention':
            try:
                validate_mention(data)
                with open(path, 'w', encoding='utf-8') as file:
                    json.dump(data, file, ensure_ascii=False)
            except:
                data['error'] = traceback.format_exc()

        return flask.render_template('review_comment.html', baseurl=config.get('site', 'baseurl'), **data)

    if 'approve' in flask.request.form or 'reject' in flask.request.form:
        approved = 'approve' in flask.request.form
        reply = flask.request.form.get('reply', '').strip()
        reply_html = format_comment(reply) if reply else ''

        if approved:
            data['comment_id'] = save_comment(data, reply_html)

        os.unlink(path)

        if reply_html and 'email_reply' in flask.request.form and data['email']:
            sender = config.get('mail', 'sender')
            send_mail(
                'comment_reply.mail', sender, data['email'],
                reply=reply_html, approved=approved, **data
            )
        if approved:
            return flask.make_response('Comment has been approved.', 200)
        else:
            return flask.make_response('Comment has been rejected.', 200)
    else:
        return flask.make_response('Invalid request', 500)

if __name__ == '__main__':
    debug = True
    app.run(host='localhost', port=5000, debug=True)
