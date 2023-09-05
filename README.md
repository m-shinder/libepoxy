[![License: MIT](https://img.shields.io/badge/license-MIT-brightgreen.svg)](https://opensource.org/licenses/MIT)

Fork of [anholt/libepoxy](https://github.com/anholt/libepoxy)

Made in order to provide vapi for Vala language.
Example of using it as a meson subproject [m-shinder/ValaGL](https://github.com/m-shinder/ValaGL)

VAPI Features
-------------

* Generated within a library and can be used as subproject.
* Everything is placed under GL namespace
* Enums are strongly typed
* GLboolean type is compatible with bool type
* If enum name contains vendor, elements of enum will not contain it.
But right constant will be used

About
-----

Epoxy is a library for handling OpenGL function pointer management for
you.

It hides the complexity of `dlopen()`, `dlsym()`, `glXGetProcAddress()`,
`eglGetProcAddress()`, etc. from the app developer, with very little
knowledge needed on their part.  They get to read GL specs and write
code using undecorated function names like `glCompileShader()`.

Don't forget to check for your extensions or versions being present
before you use them, just like before!  We'll tell you what you forgot
to check for instead of just segfaulting, though.

Features
--------

  * Automatically initializes as new GL functions are used.
  * GL 4.6 core and compatibility context support.
  * GLES 1/2/3 context support.
  * Knows about function aliases so (e.g.) `glBufferData()` can be
    used with `GL_ARB_vertex_buffer_object` implementations, along
    with GL 1.5+ implementations.
  * EGL, GLX, and WGL support.
  * Can be mixed with non-epoxy GL usage.

Building
--------

```sh
meson setup _build
cd _build
ninja
sudo ninja install
```

Dependencies for Debian:

  * meson
  * libegl1-mesa-dev

Dependencies for macOS (using MacPorts): 

  * pkgconfig
  * meson

The test suite has additional dependencies depending on the platform.
(X11, EGL, a running X Server).

Known issues when running on Windows
------------------------------------

The automatic per-context symbol resolution for win32 requires that
epoxy knows when `wglMakeCurrent()` is called, because `wglGetProcAddress()`
returns values depend on the context's device and pixel format.  If
`wglMakeCurrent()` is called from outside of epoxy (in a way that might
change the device or pixel format), then epoxy needs to be notified of
the change using the `epoxy_handle_external_wglMakeCurrent()` function.

The win32 `wglMakeCurrent()` variants are slower than they should be,
because they should be caching the resolved dispatch tables instead of
resetting an entire thread-local dispatch table every time.
