# -*- coding: utf-8 -*-
"""Main module for python-amazon-mws package."""

from collections.abc import Iterable
from enum import Enum
from io import BytesIO
from urllib.parse import quote
from xml.etree.ElementTree import ParseError as XMLError
from zipfile import ZipFile
import base64
import datetime
import hashlib
import hmac
import re
import warnings

from requests import request
from requests.exceptions import HTTPError

from mws.utils.parameters import enumerate_param
from mws.utils.collections import XML2Dict
from mws.utils.crypto import calc_md5
from mws.utils.timezone import utc_timestamp


__version__ = "1.0.0dev14"


class Marketplaces(Enum):
    """Enumeration for MWS marketplaces, containing endpoints and marketplace IDs.

    Example, endpoint and ID for UK marketplace:
        endpoint = Marketplaces.UK.endpoint
        marketplace_id = Marketplaces.UK.marketplace_id
    """

    AE = ("https://mws.amazonservices.ae", "A2VIGQ35RCS4UG")
    AU = ("https://mws.amazonservices.com.au", "A39IBJ37TRP1C6")
    BR = ("https://mws.amazonservices.com", "A2Q3Y263D00KWC")
    CA = ("https://mws.amazonservices.ca", "A2EUQ1WTGCTBG2")
    DE = ("https://mws-eu.amazonservices.com", "A1PA6795UKMFR9")
    EG = ("https://mws-eu.amazonservices.com", "ARBP9OOSHTCHU")
    ES = ("https://mws-eu.amazonservices.com", "A1RKKUPIHCS9HS")
    FR = ("https://mws-eu.amazonservices.com", "A13V1IB3VIYZZH")
    GB = ("https://mws-eu.amazonservices.com", "A1F83G8C2ARO7P")
    IN = ("https://mws.amazonservices.in", "A21TJRUUN4KGV")
    IT = ("https://mws-eu.amazonservices.com", "APJ6JRA9NG5V4")
    JP = ("https://mws.amazonservices.jp", "A1VC38T7YXB528")
    MX = ("https://mws.amazonservices.com.mx", "A1AM78C64UM0Y8")
    NL = ("https://mws-eu.amazonservices.com", "A1805IZSGTT6HS")
    SA = ("https://mws-eu.amazonservices.com", "A17E79C6D8DWNP")
    SG = ("https://mws-fe.amazonservices.com", "A19VAU5U5O7RUS")
    TR = ("https://mws-eu.amazonservices.com", "A33AVAJ2PDY3EV")
    UK = ("https://mws-eu.amazonservices.com", "A1F83G8C2ARO7P")  # alias for GB
    US = ("https://mws.amazonservices.com", "ATVPDKIKX0DER")

    def __init__(self, endpoint, marketplace_id):
        """Easy dot access like: Marketplaces.endpoint ."""
        self.endpoint = endpoint
        self.marketplace_id = marketplace_id


class MWSError(Exception):
    """Main MWS Exception class"""

    # Allows quick access to the response object.
    # Do not rely on this attribute, always check if its not None.
    response = None


def calc_request_description(params):
    """Builds the request description as a single string from the set of params.

    Each key-value pair takes the form "key=value"
    Sets of "key=value" pairs are joined by "&".
    Keys should appear in alphabetical order in the result string.

    Example:
      params = {'foo': 1, 'bar': 4, 'baz': 'potato'}
    Returns:
      "bar=4&baz=potato&foo=1"
    """
    description_items = []
    for item in sorted(params.keys()):
        encoded_val = params[item]
        description_items.append("{}={}".format(item, encoded_val))
    return "&".join(description_items)


# TODO incorporate the clean method into `RequestParameter`
def clean_params(params):
    """Input cleanup and prevent a lot of common input mistakes."""
    # silently remove parameter where values are empty
    params = {k: v for k, v in params.items() if v is not None and v != ""}

    params_enc = dict()
    for key, value in params.items():
        if isinstance(value, (dict, list, set, tuple)):
            message = (
                "expected string or datetime datatype, got {},"
                "for key {} and value {}".format(type(value), key, str(value))
            )
            raise MWSError(message)
        if isinstance(value, (datetime.datetime, datetime.date)):
            value = value.isoformat()
        if isinstance(value, bool):
            value = str(value).lower()
        value = str(value)

        params_enc[key] = quote(value, safe="-_.~")
    return params_enc


def remove_namespace(xml):
    """Strips the namespace from XML document contained in a string.
    Returns the stripped string.
    """
    regex = re.compile(' xmlns(:ns2)?="[^"]+"|(ns2:)|(xml:)')
    return regex.sub("", xml)


class DictWrapper(object):
    """Converts XML data to a parsed response object as a tree of `ObjectDict`s.

    Use `.parsed` for direct access to those contents, and `.original` for
    the original XML document string.
    """

    # TODO create a base class for DictWrapper and DataWrapper with all the keys we expect in responses.
    # This will make it easier to use either class in place of each other.
    # Either this, or pile everything into DataWrapper and make it able to handle all cases.

    def __init__(self, xml, rootkey=None):
        if isinstance(xml, bytes):
            try:
                xml = xml.decode(encoding="iso-8859-1")
            except UnicodeDecodeError as exc:
                # In the very rare occurence of a decode error, attach the original xml to the .response of the MWSError
                error = MWSError(str(exc.response.text))
                error.response = xml
                raise error

        self.response = None
        self._rootkey = rootkey
        self._mydict = XML2Dict().fromstring(remove_namespace(xml))
        self._response_dict = self._mydict.get(
            list(self._mydict.keys())[0], self._mydict
        )

    @property
    def parsed(self):
        """Returns parsed XML contents as a tree of `ObjectDict`s."""
        if self._rootkey:
            return self._response_dict.get(self._rootkey, self._response_dict)
        return self._response_dict


class DataWrapper(object):
    """Text wrapper in charge of validating the hash sent by Amazon."""

    def __init__(self, data, headers):
        self.original = data
        self.response = None
        self.headers = headers
        if "content-md5" in self.headers:
            hash_ = calc_md5(self.original)
            if self.headers["content-md5"].encode() != hash_:
                raise MWSError("Wrong Content length, maybe amazon error...")

    @property
    def parsed(self):
        """Returns original content.

        Used to provide an identical interface as `DictWrapper`, even if
        content could not be parsed as XML.
        """
        return self.original

    @property
    def unzipped(self):
        """Returns a `ZipFile` of file contents if response contains zip file bytes.

        Otherwise, returns None.
        """
        if self.headers["content-type"] == "application/zip":
            try:
                with ZipFile(BytesIO(self.original)) as unzipped_fileobj:
                    # unzipped the zip file contents
                    unzipped_fileobj.extractall()
                    # return original zip file object to the user
                    return unzipped_fileobj
            except Exception as exc:
                raise MWSError(str(exc))
        return None  # 'The response is not a zipped file.'


class MWS(object):
    """Base Amazon API class"""

    # This is used to post/get to the different uris used by amazon per api
    # ie. /Orders/2011-01-01
    # All subclasses must define their own URI only if needed
    URI = "/"

    # The API version varies in most amazon APIs
    VERSION = "2009-01-01"

    # There seem to be some xml namespace issues. therefore every api subclass
    # is recommended to define its namespace, so that it can be referenced
    # like so AmazonAPISubclass.NAMESPACE.
    # For more information see http://stackoverflow.com/a/8719461/389453
    NAMESPACE = ""

    # In here we name each of the operations available to the subclass
    # that have 'ByNextToken' operations associated with them.
    # If the Operation is not listed here, self.action_by_next_token
    # will raise an error.
    NEXT_TOKEN_OPERATIONS = []

    # Some APIs are available only to either a "Merchant" or "Seller"
    # the type of account needs to be sent in every call to the amazon MWS.
    # This constant defines the exact name of the parameter Amazon expects
    # for the specific API being used.
    # All subclasses need to define this if they require another account type
    # like "Merchant" in which case you define it like so.
    # ACCOUNT_TYPE = "Merchant"
    # Which is the name of the parameter for that specific account type.

    # For using proxy you need to init this class with one more parameter proxies. It must look like 'ip_address:port'
    # if proxy without auth and 'login:password@ip_address:port' if proxy with auth

    ACCOUNT_TYPE = "SellerId"

    def __init__(
        self,
        access_key,
        secret_key,
        account_id,
        region="US",
        uri="",
        version="",
        auth_token="",
        proxy=None,
    ):
        self.access_key = access_key
        self.secret_key = secret_key
        self.account_id = account_id
        self.auth_token = auth_token
        self.version = version or self.VERSION
        self.uri = uri or self.URI
        self.proxy = proxy

        # * TESTING FLAGS * #
        self._test_request_params = False

        if region in Marketplaces.__members__:
            self.domain = Marketplaces[region].endpoint
        else:
            error_msg = (
                "Incorrect region supplied: {region}. "
                "Must be one of the following: {regions}".format(
                    region=region, regions=", ".join(Marketplaces.__members__.keys()),
                )
            )
            raise MWSError(error_msg)

    def get_default_params(self):
        """Get the parameters required in all MWS requests."""
        params = {
            "AWSAccessKeyId": self.access_key,
            self.ACCOUNT_TYPE: self.account_id,
            "SignatureVersion": "2",
            "Timestamp": utc_timestamp(),
            "Version": self.version,
            "SignatureMethod": "HmacSHA256",
        }
        if self.auth_token:
            params["MWSAuthToken"] = self.auth_token
        # TODO current tests only check for auth_token being set.
        # need a branch test to check for auth_token being skipped (no key present)
        return params

    def make_request(self, extra_data, method="GET", **kwargs):
        """Make request to Amazon MWS API with these parameters."""
        params = self.get_default_params()
        proxies = self.get_proxies()
        params.update(extra_data)
        params = clean_params(params)

        if self._test_request_params:
            # Testing method: return the params from this request before the request is made.
            return params
        # TODO: All current testing stops here. More branches needed.

        request_description = calc_request_description(params)
        signature = self.calc_signature(method, request_description)
        url = "{domain}{uri}?{description}&Signature={signature}".format(
            domain=self.domain,
            uri=self.uri,
            description=request_description,
            signature=quote(signature),
        )
        headers = {
            "User-Agent": "python-amazon-mws/{} (Language=Python)".format(__version__)
        }
        headers.update(kwargs.get("extra_headers", {}))

        try:
            # Some might wonder as to why i don't pass the params dict as the params argument to request.
            # My answer is, here i have to get the url parsed string of params in order to sign it, so
            # if i pass the params dict as params to request, request will repeat that step because it will need
            # to convert the dict to a url parsed string, so why do it twice if i can just pass the full url :).
            response = request(
                method,
                url,
                data=kwargs.get("body", ""),
                headers=headers,
                proxies=proxies,
                timeout=kwargs.get("timeout", 300),
            )
            response.raise_for_status()
            # When retrieving data from the response object,
            # be aware that response.content returns the content in bytes while response.text calls
            # response.content and converts it to unicode.

            data = response.content
            # I do not check the headers to decide which content structure to server simply because sometimes
            # Amazon's MWS API returns XML error responses with "text/plain" as the Content-Type.
            rootkey = kwargs.get("rootkey", extra_data.get("Action") + "Result")
            try:
                try:
                    parsed_response = DictWrapper(data, rootkey)
                except TypeError:  # raised when using Python 3 and trying to remove_namespace()
                    # When we got CSV as result, we will got error on this
                    parsed_response = DictWrapper(response.text, rootkey)

            except XMLError:
                parsed_response = DataWrapper(data, response.headers)

        except HTTPError as exc:
            error = MWSError(str(exc.response.text))
            error.response = exc.response
            raise error

        # Store the response object in the parsed_response for quick access
        parsed_response.response = response
        return parsed_response

    def get_proxies(self):
        """Return a dict of http and https proxies, as defined by `self.proxy`."""
        proxies = {"http": None, "https": None}
        if self.proxy:
            # TODO need test to enter here
            proxies = {
                "http": "http://{}".format(self.proxy),
                "https": "https://{}".format(self.proxy),
            }
        return proxies

    def get_service_status(self):
        """Returns MWS service status.

        Typical return values (embedded within `response.parsed`) are:

        - GREEN
        - GREEN_I
        - YELLOW
        - RED

        The same request can be used for any MWS API subclass, and MWS may respond
        differently for each endpoint. Best to use this method from the same API
        subclass you intend to use for other requests!

        Docs (from Orders API example):
        http://docs.developer.amazonservices.com/en_US/orders-2013-09-01/MWS_GetServiceStatus.html
        """
        return self.make_request(extra_data=dict(Action="GetServiceStatus"))

    def action_by_next_token(self, action, next_token):
        """Run a '...ByNextToken' action for the given action.

        If the action is not listed in self.NEXT_TOKEN_OPERATIONS, MWSError is raised.
        Action is expected NOT to include 'ByNextToken'
        at the end of its name for this call: function will add that by itself.
        """
        if action not in self.NEXT_TOKEN_OPERATIONS:
            # TODO Would like a test entering here.
            # Requires a dummy API class to be written that will trigger it.
            raise MWSError(
                (
                    "{} action not listed in this API's NEXT_TOKEN_OPERATIONS. "
                    "Please refer to documentation."
                ).format(action)
            )

        action = "{}ByNextToken".format(action)

        data = {
            "Action": action,
            "NextToken": next_token,
        }
        return self.make_request(data, method="POST")

    def calc_signature(self, method, request_description):
        """Calculate MWS signature to interface with Amazon

        Args:
            method (str)
            request_description (str)
        """
        sig_data = "\n".join(
            [
                method,
                self.domain.replace("https://", "").lower(),
                self.uri,
                request_description,
            ]
        )
        return base64.b64encode(
            hmac.new(
                self.secret_key.encode(), sig_data.encode(), hashlib.sha256
            ).digest()
        )

    def enumerate_param(self, param, values):
        """DEPRECATED, alias for `utils.parameters.enumerate_param`."""
        # TODO remove in 1.0 release.
        # No tests needed.
        warnings.warn(
            (
                "Please use `utils.parameters.enumerate_param` for one param, or "
                "`utils.parameters.enumerate_params` for multiple params."
            ),
            DeprecationWarning,
        )
        return enumerate_param(param, values)

    def generic_request(self, action, parameters=None, method="GET", **kwargs):
        """Builds a generic request with arbitrary parameter arguments.

        `action` is a string matching the name of the request action
        (i.e. "ListOrders").

        `parameters` must be a dict, and is passed to `RequestParameter` as `value`
        (no `key` arg is used). See docs for `RequestParameter` for details.

        Set `method` to `"POST"` to send this request via POST. Defaults to `"GET"`.

        `kwargs` are passed unchanged to `make_request`.
        """
        if not self.uri or self.uri == "/":
            raise ValueError(
                (
                    "Cannot send generic request to URI '%s'. "
                    "Please use one of the API classes "
                    "(`mws.apis.Reports`, `mws.apis.Feeds`, etc.) "
                    "to initiate this request."
                )
                % self.uri
            )
        if not isinstance(parameters, dict):
            raise ValueError("`parameters` must be a dict.")
        data = {"Action": action}
        data.update(RequestParameter(value=parameters).to_dict())
        return self.make_request(data, method=method, **kwargs)


# TODO Move this to its own module when things are being reworked!
class RequestParameter:
    """An MWS request parameter, defined by a `key` string and a `value`.

    Using this object with its `to_dict` method, any arbitrarily-nested list or dict
    in `value` will be expanded and flattened, such that each key in a sub-dict is
    concatenated to `key` as a dotted string; and each element in a list is
    enumerated (starting from 1) and concatenated to `key`, also as a dotted string.

    Example:
        value = {
            "a": 1,
            "b": "hello",
            "c": [
                "foo",
                "bar",
                {
                    "what": "have",
                    "you": [
                        5,
                        6,
                        7,
                    ],
                },
            ],
        }
        print(RequestParameter(key="example", value=value).to_dict())
        # Formatted for readability:
        >>> {
            "example.a": 1,
            "example.b": "hello",
            "example.c.1": "foo",
            "example.c.2": "bar",
            "example.c.3.what": "have",
            "example.c.3.you.1": 5,
            "example.c.3.you.2": 6,
            "example.c.3.you.3": 7,
        }

    - The parameter key "example" is placed in front of each new key.
      - An empty `key` can also be used when `value` is a nested object:
        `RequestParameter(value=value)`.
        This will output the same as above, without `example.` in front of each key.
      - When using an empty `key`, having a `value` that is not a dict and
        not a non-string iterable raises ValueError
    - "a" and "b" are simple values, and are returned.
    - "c" contains an iterable (list), which is enumerated with a 1-based index.
      These are joined to "c" with ".", creating keys "c.1" and "c.2".
    - At "c.3", another nested object is located. This is passed recursively to a new
      `RequestParameter`, and the same process repeats (dicts are keyed, iterables
      are enumerated with 1-based index, and simple values are returned).
    - The same occurs for "c.3.you", where an iterable is found and is enumerated.
    - The final output should always be a flat dictionary with key-value pairs.

    The output of `to_dict` is used within the `to_str` method to create a single
    string value from all key-value pairs (keys and values joined by "=",
    and pairs joined by "&").
    """

    def __init__(self, key=None, value=None):
        self.key = key
        self.value = value
        self.validate()

    def validate(self):
        if not self.key and not self._val_is_dict() and not self._val_is_iterable():
            raise ValueError(
                "Parameter with empty `key` must have a dict or iterable `value`."
            )

    def _val_is_str(self):
        """Return bool, whether the value is a string.

        Used solely in the `_val_is_iterable` test.
        """
        return isinstance(self.value, str)

    def _val_is_dict(self):
        """Return bool, whether the value is a dict."""
        return isinstance(self.value, dict)

    def _val_is_iterable(self):
        """Return bool, whether the value is a "psuedo-iterable".

        As a special case, returns False if the value is a dict or str,
        because we want to treat those objects differently.
        """
        if self._val_is_dict() or self._val_is_str():
            return False
        return isinstance(self.value, Iterable)

    def to_dict(self):
        """Return a flat dict, 1 level deep, by enumerating or keying nested
        lists and dicts in `self.value`.
        """
        if self.value is None:
            # Returns nothing for a `None` value
            return {}
        if self._val_is_dict():
            return self.keyed_value()
        if self._val_is_iterable():
            return self.enumerated_value()
        return {self.key: self.value}

    def to_str(self):
        """Converts the output of `to_dict` to a string.

        Each key-value pair in the flattened dict is output as "key=val",
        and all pairs are joined by "&".
        """
        content = self.to_dict()
        output = []
        for key, val in content.items():
            output.append("{}={}".format(key, val))
        return "&".join(output)

    @property
    def param_key(self):
        """Outputs the key to use when outputting nested parameters.

        If the key is not set, returns an empty string.
        Otherwise returns the key string, ensuring there is a "." appended to it.
        """
        param = ""
        if self.key:
            param = self.key
            if not param.endswith("."):
                param += "."
        return param

    def keyed_value(self):
        """Returns a flat dict for a nested dict `value`.

        For each `sub_key`/`sub_val` pair of the `value` dict,
        `sub_key` is combined with this parameter's `key` and "." to create a new key.

        This new key and `sub_val` are then passed to a new `RequestParameter` object,
        where the output of that sub-parameter's `to_dict()` method is recursively
        added back to the return value dictionary here.
        """
        if not self._val_is_dict:
            raise ValueError("Cannot generate keyed value for non-dict `value`.")
        output = {}
        for sub_key, val in self.value.items():
            new_key = "{}{}".format(self.param_key, sub_key)
            # Update our output with the dict from a nested parameter
            # using this new key and value.
            output.update(RequestParameter(key=new_key, value=val).to_dict())
        return output

    def enumerated_value(self):
        """Returns a flat dict for a nested iterable `value` (similar to `keyed_value`).

        Each element of the `value` iterable is enumerated with a 1-based index `idx`.
        `idx` is then joined to `key` with "." to obtain a new key.

        From there, the same recursive methodology as `keyed_value` is used,
        generating a sub-parameter and dict output that is added back
        to the return value dictionary.
        """
        if not self._val_is_iterable:
            raise ValueError(
                "Cannot generate enumerated value for non-iterable `value`."
            )
        output = {}
        for idx, val in enumerate(self.value):
            new_key = "{}{}".format(self.param_key, idx + 1)
            # Update our output with the dict from a nested parameter
            # using this new key and value.
            output.update(RequestParameter(key=new_key, value=val).to_dict())
        return output
