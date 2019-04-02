import bleach
import markdown

formatter = markdown.Markdown(output_format='html5')
del formatter.parser.blockprocessors['hashheader']
del formatter.parser.blockprocessors['setextheader']
del formatter.inlinePatterns['image_link']
del formatter.inlinePatterns['image_reference']

# The way to import Filter class changes between bleach 2.x and bleach 3.x,
# this approach should work with both.
class NofollowFilter(bleach.sanitizer.BleachSanitizerFilter.__bases__[0]):
    def __iter__(self):
        for token in super().__iter__():
            if token['type'] in ['StartTag', 'EmptyTag'] and token['name'] == 'a' and token['data']:
                token['data'][('', 'rel')] = 'nofollow'
            yield token


cleaner = bleach.sanitizer.Cleaner(
    tags=['p', 'br', 'hr', 'pre'] + bleach.sanitizer.ALLOWED_TAGS,
    filters=[NofollowFilter]
)

def format_comment(text):
    html = formatter.convert(text)
    return cleaner.clean(html)
