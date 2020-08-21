"""Parameter manipulation utilities."""

from collections.abc import Iterable, Mapping
from urllib.parse import quote
import datetime
import json

from mws.errors import MWSError


def enumerate_param(param, values):
    """Builds a dictionary of an enumerated parameter, using the param string and some values.
    If values is not a list, tuple, or set, it will be coerced to a list
    with a single item.

    Example:
        enumerate_param('MarketplaceIdList.Id', (123, 345, 4343))
    Returns:
        {
            MarketplaceIdList.Id.1: 123,
            MarketplaceIdList.Id.2: 345,
            MarketplaceIdList.Id.3: 4343
        }
    """
    if not isinstance(values, (list, tuple, set)):
        # Coerces a single value to a list before continuing.
        values = [values]
    if not any(values):
        # if not values -> returns ValueError
        return {}
    if not param.endswith("."):
        # Ensure this enumerated param ends in '.'
        param += "."
    # Return final output: dict comprehension of the enumerated param and values.
    return {"{}{}".format(param, idx + 1): val for idx, val in enumerate(values)}


def enumerate_params(params=None):
    """For each param and values, runs enumerate_param,
    returning a flat dict of all results
    """
    if params is None or not isinstance(params, dict):
        return {}
    params_output = {}
    for param, values in params.items():
        params_output.update(enumerate_param(param, values))
    return params_output


def enumerate_keyed_param(param, values):
    """Given a param string and a dict of values, returns a flat dict of keyed, enumerated params.
    Each dict in the values list must pertain to a single item and its data points.

    Example:
        param = "InboundShipmentPlanRequestItems.member"
        values = [
            {'SellerSKU': 'Football2415',
            'Quantity': 3},
            {'SellerSKU': 'TeeballBall3251',
            'Quantity': 5},
            ...
        ]

    Returns:
        {
            'InboundShipmentPlanRequestItems.member.1.SellerSKU': 'Football2415',
            'InboundShipmentPlanRequestItems.member.1.Quantity': 3,
            'InboundShipmentPlanRequestItems.member.2.SellerSKU': 'TeeballBall3251',
            'InboundShipmentPlanRequestItems.member.2.Quantity': 5,
            ...
        }
    """
    if not isinstance(values, (list, tuple, set)):
        # If it's a single value, convert it to a list first
        values = [values]
    if not any(values):
        # Shortcut for empty values
        return {}
    if not param.endswith("."):
        # Ensure the enumerated param ends in '.'
        param += "."
    for val in values:
        # Every value in the list must be a dict.
        if not isinstance(val, dict):
            # Value is not a dict: can't work on it here.
            raise ValueError(
                (
                    "Non-dict value detected. "
                    "`values` must be a list, tuple, or set; containing only dicts."
                )
            )
    params = {}
    for idx, val_dict in enumerate(values):
        # Build the final output.
        params.update(
            {
                "{param}{idx}.{key}".format(param=param, idx=idx + 1, key=k): v
                for k, v in val_dict.items()
            }
        )
    return params


def dict_keyed_param(param, dict_from):
    """Given a param string and a dict, returns a flat dict of keyed params without enumerate.

    Example:
        param = "ShipmentRequestDetails.PackageDimensions"
        dict_from = {'Length': 5, 'Width': 5, 'Height': 5, 'Unit': 'inches'}

    Returns:
        {
            'ShipmentRequestDetails.PackageDimensions.Length': 5,
            'ShipmentRequestDetails.PackageDimensions.Width': 5,
            'ShipmentRequestDetails.PackageDimensions.Height': 5,
            'ShipmentRequestDetails.PackageDimensions.Unit': 'inches',
            ...
        }
    """
    params = {}

    if not param.endswith("."):
        # Ensure the enumerated param ends in '.'
        param += "."
    for k, v in dict_from.items():
        params.update({"{param}{key}".format(param=param, key=k): v})
    return params


def clean_params_dict(params):
    """Clean multiple param values in a dict, returning a new dict
    containing the original keys and cleaned values.
    """
    # silently remove parameter where values are empty
    params = {k: v for k, v in params.items() if v is not None and v != ""}

    cleaned_params = dict()
    for key, val in params.items():
        try:
            cleaned_params[key] = clean_value(val)
        except ValueError as exc:
            raise MWSError(str(exc)) from exc
    return cleaned_params


def clean_value(val):
    """Attempts to clean a value so that it can be sent in a request."""
    if isinstance(val, (dict, list, set, tuple)):
        raise ValueError("Cannot clean parameter value of type %s" % str(type(val)))

    if isinstance(val, (datetime.datetime, datetime.date)):
        return clean_date(val)
    if isinstance(val, bool):
        return clean_bool(val)

    # For all else, assume a string, and clean that.
    return clean_string(str(val))


def clean_string(val):
    """Passes a string value through `urllib.parse.quote` to clean it.

    Safe characters permitted: -_.~
    """
    return quote(val, safe="-_.~")


def clean_bool(val):
    """Converts a boolean value to its JSON string equivalent."""
    if val is not True and val is not False:
        raise ValueError("Expected a boolean, got %s" % val)
    return json.dumps(val)


def clean_date(val):
    """Converts a datetime.datetime or datetime.date to ISO 8601 string.
    Further passes that string through `urllib.parse.quote`.
    """
    return clean_string(val.isoformat())


def flat_param_dict(value, prefix=""):
    """Returns a flattened params dictionary by collapsing nested dicts and
    non-string iterables.

    Any arbitrarily-nested dict or iterable will be expanded and flattened.

    - Each key in a child dict will be concatenated to its parent key.
    - Elements of a non-string iterable will be enumerated using a 1-based index,
      with the index number concatenated to the parent key.
    - In both cases, keys are joined with '.'.

    Example:
        value = {
            "a": 1,
            "b": "hello",
            "c": [
                "foo",
                "bar",
                {
                    "spam": "ham",
                    "eggs": [
                        5,
                        6,
                        7,
                    ],
                },
            ],
        }
        print(flat_param_dict(value))
        # Formatted for readability:
        >>> {
            "a": 1,
            "b": "hello",
            "c.1": "foo",
            "c.2": "bar",
            "c.3.spam": "ham",
            "c.3.eggs.1": 5,
            "c.3.eggs.2": 6,
            "c.3.eggs.3": 7,
        }

    - "a" and "b" keys point to non-dict, non-sequence values (not including strings),
      so they return their original values.
    - "c" contains an iterable (list), which is enumerated with a 1-based index.
      Each index is concatenated to "c" with ".", creating keys "c.1" and "c.2".
    - At "c.3", another nested object was found. This is processed recursively,
      and each key of the resulting dict is concatenated to the parent "c.3"
      to create multiple keys in the final output.
    - The same occurs for "c.3.eggs", where an iterable is found and is enumerated.
    - The final output should always be a flat dictionary with key-value pairs.

    If `prefix` is provided, it will be prepended to each key in the flat dict:

        print(flat_param_dict(value, prefix="example"))
        >>> {
            "example.a": 1,
            "example.b": "hello",
            "example.c.1": "foo",
            "example.c.2": "bar",
            "example.c.3.spam": "ham",
            "example.c.3.eggs.1": 5,
            "example.c.3.eggs.2": 6,
            "example.c.3.eggs.3": 7,
        }
    """
    prefix = "" if not prefix else str(prefix)
    # Prefix is now either an empty string or a valid prefix string ending in '.'
    # NOTE should ensure that a `None` value is changed to empty string, as well.

    if isinstance(value, str) or not isinstance(value, (dict, Iterable)):
        # Value is not one of the types we want to expand.
        if prefix:
            # Can return a single dict of the prefix and value as a base case
            return {prefix: value}
        raise ValueError(
            (
                "Non-dict, non-iterable value requires a prefix "
                "(would return a mapping of `prefix: value`)"
            )
        )

    # Past here, the value is something that must be expanded.
    # We'll build that output with recursive calls to `flat_param_dict`.

    if prefix and not prefix.endswith("."):
        prefix += "."

    output = {}
    if isinstance(value, dict):
        for key, val in value.items():
            new_key = "{}{}".format(prefix, key)
            output.update(flat_param_dict(val, prefix=new_key))
    else:
        # value must be an Iterable
        for idx, val in enumerate(value):
            new_key = "{}{}".format(prefix, idx + 1)
            output.update(flat_param_dict(val, prefix=new_key))
    return output