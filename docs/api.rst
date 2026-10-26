API Reference
=============

.. module:: marko

Main Class
----------

.. autoclass:: Markdown
    :inherited-members:


Parser Class
------------

.. module:: marko.parser

.. autoclass:: Parser
    :inherited-members:

Renderer Class
--------------

.. module:: marko.renderer

.. autoclass:: Renderer
    :inherited-members:

.. _elements:

Elements
--------

Block Elements
~~~~~~~~~~~~~~

.. autoclass:: marko.block.BlockElement
    :members:

.. automodule:: marko.block
    :members:

Inline Elements
~~~~~~~~~~~~~~~

.. autoclass:: marko.inline.InlineElement
    :members:

.. automodule:: marko.inline
    :members:

Helper Classes
--------------

.. autoclass:: marko.element.Element
    :members:

.. autoclass:: marko.source.Source
    :members:

.. autoclass:: marko.helpers.MarkoExtension
    :members:

Localization
------------

.. module:: marko.l10n

.. autoclass:: Catalog
    :members:

.. autoclass:: CatalogEntry
    :members:

.. autoclass:: Segment
    :members:

.. autoclass:: Placeholder
    :members:

.. autoexception:: CatalogError
    :members:

.. autofunction:: export_catalog

.. autofunction:: apply_translations

