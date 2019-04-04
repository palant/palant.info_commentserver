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
import urllib

import flask
import frontmatter
import jinja2

from format import format_comment

basedir = os.path.dirname(sys.argv[0]) or '.'
config = configparser.ConfigParser()
config.read(os.path.join(basedir, 'config.ini'))

app = flask.Flask(
    'comment_management',
    template_folder=os.path.join(basedir, 'templates')
)

def add_header(name, value):
    def decorator(func):
        def wrapper(*args):
            response = func(*args)
            response.headers[name] = value
            return response
        return wrapper
    return decorator


def resolve_path(path):
    if not os.path.isabs(path):
        path = os.path.join(basedir, path)
    return os.path.abspath(path)


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
    old_master = github_request('GET', 'commits/master')

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
            'id': reply_id
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
        'base_tree': old_master['commit']['tree']['sha'],
        'tree': tree
    })['sha']

    commit = github_request('POST', 'git/commits', {
        'message': 'Added blog comment',
        'tree': tree,
        'parents': [old_master['sha']],
    })['sha']

    github_request('PATCH', 'git/refs/heads/master', {
        'sha': commit
    })

    if config.has_option('hook', 'postupdate'):
        time.sleep(5)
        subprocess.check_call(config.get('hook', 'postupdate'), shell=True)

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
        sender = config.get('mail', 'sender')
        smtp.sendmail(sender, sender, message.encode('utf-8'))


@app.route('/comment/submit', methods=['POST', 'OPTIONS'])
@add_header('Access-Control-Allow-Origin', 'http://localhost:1313')
@add_header('Access-Control-Allow-Headers', 'X-XMLHttpRequest')
def submit():
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


@app.route('/comment/review/<id>', methods=['GET', 'POST'])
def review_comment(id):
    if not re.match(r'^[\da-f]+$', id):
        return flask.make_response('Invalid ID', 500)

    path = os.path.join(get_queue_dir(), id)
    with open(path, 'r', encoding='utf-8') as file:
        data = json.load(file)

    if flask.request.method == 'GET':
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
    app.run(host='localhost', port=5000, debug=True)
