from django import template
from django.http import QueryDict

register = template.Library()


@register.simple_tag(takes_context=True)
def query_string(context, **kwargs):
    """
    The current query string with some parameters replaced, e.g. for page links:

        <a href="?{% query_string page=3 %}">

    Every other parameter (search fields, ?filter=...) is kept, so going to the next page
    does not throw the user's filters away. A parameter given as None or '' is removed.
    """
    request = context.get('request')
    params = request.GET.copy() if request is not None else QueryDict(mutable=True)

    for key, value in kwargs.items():
        if value is None or value == '':
            params.pop(key, None)
        else:
            params[key] = value

    return params.urlencode()


@register.simple_tag
def page_items(page_obj):
    """
    A short list of page links with gaps, e.g. 1 … 4 [5] 6 … 20, ready for a template.
    Every item is a dict: {'gap': True} or {'number': n, 'current': bool}.
    """
    paginator = page_obj.paginator
    items = []
    for number in paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1):
        if isinstance(number, int):
            items.append({'number': number, 'current': number == page_obj.number})
        else:
            items.append({'gap': True})
    return items
