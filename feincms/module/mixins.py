import re

from django.db import models
from django.http import Http404
from django.template import Template
from django.utils.cache import add_never_cache_headers
from django.utils.datastructures import SortedDict
from django.views.generic import TemplateView

from feincms import settings


class ContentMixin(object):
    """
    Mixin for ``feincms.models.Base`` subclasses which need need some degree of
    additional control over the request-response cycle.
    """

    #: Collection of request processors
    request_processors = SortedDict()

    #: Collection of response processors
    response_processors = SortedDict()

    def setup_request(self, request):
        import warnings
        warnings.warn(
            '%s.setup_request does nothing anymore, and will be removed in'
            ' FeinCMS v1.8',
            DeprecationWarning, stacklevel=2)

    @classmethod
    def register_request_processor(cls, fn, key=None):
        """
        Registers the passed callable as request processor. A request processor
        always receives two arguments, the current object and the request.
        """
        cls.request_processors[fn if key is None else key] = fn

    @classmethod
    def register_response_processor(cls, fn, key=None):
        """
        Registers the passed callable as response processor. A response
        processor always receives three arguments, the current object, the
        request and the response.
        """
        cls.response_processors[fn if key is None else key] = fn


class ContentView(TemplateView):
    #: The name of the object for the template rendering context
    context_object_name = 'feincms_object'

    def get(self, request, *args, **kwargs):
        return self.handler(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.handler(request, *args, **kwargs)

    def handler(self, request, *args, **kwargs):
        self.page = Page.objects.for_request(request,
            raise404=True, best_match=True)

    def handle_object(self, object):
        if not hasattr(self.request, '_feincms_extra_context'):
            self.request._feincms_extra_context = {}

        self.object = object

        r = self.run_request_processors()
        if r:
            return r

        r = self.process_content_types()
        if r:
            return r

        response = self.render_to_response(self.get_context_data())

        r = self.finalize_content_types(response)
        if r:
            return r

        r = self.run_response_processors(response)
        if r:
            return r

        return response

    def get_template_names(self):
        # According to the documentation this method is supposed to return
        # a list. However, we can also return a Template instance...
        if isinstance(self.template_name, (Template, list, tuple)):
            return self.template_name

        if self.template_name:
            return [self.template_name]

        # Hopefully someone run register_templates on the object class
        # beforehand, otherwise we'll crash...
        return [self.object.template.path]

    def get_context_data(self, **kwargs):
        context = self.request._feincms_extra_context
        context[self.context_object_name] = self.object
        return context

    @property
    def __name__(self):
        """
        Dummy property to make this handler behave like a normal function.
        This property is used by django-debug-toolbar
        """
        return self.__class__.__name__

    def run_request_processors(self):
        """
        Before rendering an object, run all registered request processors. A
        request processor may peruse and modify the page or the request. It can
        also return a ``HttpResponse`` for shortcutting the rendering and
        returning that response immediately to the client.
        """

        self.request._feincms_extra_context.update({
            # XXX This variable name isn't accurate anymore.
            # We _are_ in a subpage, but it isn't necessarily
            # an appcontent subpage.
            'in_appcontent_subpage': False,
            'extra_path': '/',
            })

        url = self.object.get_absolute_url()
        if self.request.path != url:
            # extra_path must not end with a slash
            self.request._feincms_extra_context.update({
                'in_appcontent_subpage': True,
                'extra_path': re.sub('^' + re.escape(url.rstrip('/')), '',
                    self.request.path),
                })

        for fn in reversed(self.object.request_processors.values()):
            r = fn(self.object, self.request)
            if r:
                return r

    def run_response_processors(self, response):
        """
        After rendering an object to a response, the registered response
        processors are called to modify the response, eg. for setting cache or
        expiration headers, keeping statistics, etc.
        """
        for fn in self.object.response_processors.values():
            r = fn(self.object, self.request, response)
            if r:
                return r

    def process_content_types(self):
        """
        Run the ``process`` method of all content types sporting one
        """
        # store eventual Http404 exceptions for re-raising,
        # if no content type wants to handle the current self.request
        http404 = None
        # did any content type successfully end processing?
        successful = False

        for content in self.object.content.all_of_type(tuple(
                self.object._feincms_content_types_with_process)):

            try:
                r = content.process(self.request, view=self)
                if r in (True, False):
                    successful = r
                elif r:
                    return r
            except Http404, e:
                http404 = e

        if not successful:
            if http404:
                # re-raise stored Http404 exception
                raise http404

            if not settings.FEINCMS_ALLOW_EXTRA_PATH and \
                    self.request._feincms_extra_context['extra_path'] != '/':
                raise Http404

    def finalize_content_types(self, response):
        """
        Runs finalize() on content types having such a method, adds headers and
        returns the final response.
        """

        for content in self.object.content.all_of_type(tuple(
                self.object._feincms_content_types_with_finalize)):

            r = content.finalize(self.request, response)
            if r:
                return r

        # Add never cache headers in case frontend editing is active
        if (hasattr(self.request, "COOKIES")
                and self.request.COOKIES.get('frontend_editing', False)):

            if hasattr(response, 'add_post_render_callback'):
                response.add_post_render_callback(add_never_cache_headers)
            else:
                add_never_cache_headers(response)
