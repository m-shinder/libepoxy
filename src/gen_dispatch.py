#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2013 Intel Corporation
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice (including the next
# paragraph) shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

from functools import reduce
import sys
import argparse
import xml.etree.ElementTree as ET
import re
import os

class Typedef(object):
    def __init__(self, tag):
        self.prefix = tag.text or ''

        self.is_apientry = True if tag.find('apientry') != None else False

        if (name := tag.find('name')) != None:
            self.name = name.text
            self.postfix = name.tail
        else:
            self.name = self.postfix = ''
    
    def __str__(self):
        apientry = 'APIENTRY *' if self.is_apientry else '';
        return self.prefix + apientry + self.name + self.postfix + '\n'



class GLProvider(object):
    def __init__(self, condition, condition_name, loader, name):
        # C code for determining if this function is available.
        # (e.g. epoxy_is_desktop_gl() && epoxy_gl_version() >= 20
        self.condition = condition

        # A string (possibly with spaces) describing the condition.
        self.condition_name = condition_name

        # The loader for getting the symbol -- either dlsym or
        # getprocaddress.  This is a python format string to generate
        # C code, given self.name.
        self.loader = loader

        # The name of the function to be loaded (possibly an
        # ARB/EXT/whatever-decorated variant).
        self.name = name

        # This is the C enum name we'll use for referring to this provider.
        self.enum = condition_name
        self.enum = self.enum.replace(' ', '_')
        self.enum = self.enum.replace('\\"', '')
        self.enum = self.enum.replace('.', '_')
        self.enum = "PROVIDER_" + self.enum

class GLFunction(object):
    @property
    def args_list(self):
        # Mac screwed up GLhandleARB and made it a void * instead of
        # uint32_t, despite it being specced as only necessarily 32
        # bits wide, causing portability problems all over.  There are
        # prototype conflicts between things like
        # glAttachShader(GLuint program, GLuint shader) and
        # glAttachObjectARB(GLhandleARB container, GLhandleARB obj),
        # even though they are marked as aliases in the XML (and being
        # aliases in Mesa).
        #
        # We retain those aliases.  In the x86_64 ABI, the first 6
        # args are stored in 64-bit registers, so the calls end up
        # being the same despite the different types.  We just need to
        # add a cast to uintptr_t to shut up the compiler.
        result = ''
        arg_number = 0
        for arg in self.args:
            if arg['type'] == 'GLhandleARB':
                assert arg_number < 6
                arg_list_name = '(uintptr_t)' + arg['name']
            else:
                arg_list_name = arg['name']

            if result != '':
                result += ', '
            result += arg_list_name
            arg_number += 1

        return result

    @property
    def args_decl(self):
        result = ', '.join(list(map(
                lambda arg: arg['type'] + ' ' + arg['name'],
                self.args
            )))
        if result != '':
            return result
        else:
            return 'void'

    def __init__(self, ret_type, name):
        self.name = name
        self.ptr_type = 'PFN' + name.upper() + 'PROC'
        self.ret_type = ret_type
        self.providers = {}
        self.args = []

        # These are functions with hand-written wrapper code in
        # dispatch_common.c.  Their dispatch entries are replaced with
        # non-public symbols with a "_unwrapped" suffix.
        wrapped_functions = {
            'glBegin',
            'glEnd',
            'wglMakeCurrent',
            'wglMakeContextCurrentEXT',
            'wglMakeContextCurrentARB',
            'wglMakeAssociatedContextCurrentAMD',
        }

        if name in wrapped_functions:
            self.wrapped_name = name + '_unwrapped'
            self.public = ''
        else:
            self.wrapped_name = name
            self.public = 'EPOXY_PUBLIC '

        # This is the string name of the function that this is an
        # alias of, or self.name.  This initially comes from the
        # registry, and may get updated if it turns out our alias is
        # itself an alias (for example glFramebufferTextureEXT ->
        # glFramebufferTextureARB -> glFramebufferTexture)
        self.alias_name = name

        # After alias resolution, this is the function that this is an
        # alias of.
        self.alias_func = None

        # For the root of an alias tree, this lists the functions that
        # are marked as aliases of it, so that it can write a resolver
        # for all of them.
        self.alias_exts = []

    def add_arg(self, arg_type, arg_name, **kwargs):
        # Reword glDepthRange() arguments to avoid clashing with the
        # "near" and "far" keywords on win32.
        if arg_name == "near":
            arg_name = "hither"
        elif arg_name == "far":
            arg_name = "yon"

        arg = {
            'type': arg_type,
            'name': arg_name,
        }
        if arg_type in [ 'GLenum', 'GLbitfield' ]:
            if 'group' in kwargs:
                arg['group'] = kwargs['group']
            else:
                arg['group'] = None
        self.args.append(arg)

    def add_provider(self, condition, loader, condition_name):
        self.providers[condition_name] = GLProvider(condition, condition_name,
                                                    loader, self.name)

    def add_alias(self, ext):
        assert self.alias_func is None

        self.alias_exts.append(ext)
        ext.alias_func = self

class Generator(object):
    def __init__(self, target):
        self.target = target
        self.groups = {}
        self.enums = {}
        self.typedefs = []
        self.functions = {}
        self.sorted_functions = []
        self.enum_string_offset = {}
        self.max_enum_name_len = 1
        self.entrypoint_string_offset = {}
        self.copyright_comment = None
        self.typedefs_string = ''
        self.out_file = None

        # GL versions named in the registry, which we should generate
        # #defines for.
        self.supported_versions = set()

        # Extensions named in the registry, which we should generate
        # #defines for.
        self.supported_extensions = set()

        # Dictionary mapping human-readable names of providers to a C
        # enum token that will be used to reference those names, to
        # reduce generated binary size.
        self.provider_enum = {}

        # Dictionary mapping human-readable names of providers to C
        # code to detect if it's present.
        self.provider_condition = {}

        # Dictionary mapping human-readable names of providers to
        # format strings for fetching the function pointer when
        # provided the name of the symbol to be requested.
        self.provider_loader = {}

    def all_text_until_element_name(self, element, element_name):
        text = ''

        if element.text is not None:
            text += element.text

        for child in element:
            if child.tag == element_name:
                break
            if child.text:
                text += child.text
            if child.tail:
                text += child.tail
        return text

    def out(self, text):
        self.out_file.write(text)

    def outln(self, text):
        self.out_file.write(text + '\n')

    def parse_typedefs(self, reg):
        for t in reg.findall('types/type'):
            if 'name' in t.attrib and t.attrib['name'] not in {'GLhandleARB'}:
                continue

            # The gles1/gles2-specific types are redundant
            # declarations, and the different types used for them (int
            # vs int32_t) caused problems on win32 builds.
            api = t.get('api')
            if api:
                continue

            self.typedefs.append(Typedef(t))
        self.typedefs_string = reduce(
                lambda x,y: x+y,
                map(str, self.typedefs),
                self.typedefs_string
            )

    def parse_enums(self, reg):
        for enum in reg.findall('enums/enum'):
            name = enum.get('name')

            # wgl.xml's 0xwhatever definitions end up colliding with
            # wingdi.h's decimal definitions of these.
            if name in ['WGL_SWAP_OVERLAY', 'WGL_SWAP_UNDERLAY', 'WGL_SWAP_MAIN_PLANE']:
                continue

            self.max_enum_name_len = max(self.max_enum_name_len, len(name))
            self.enums[name] = enum.get('value')

            groups = enum.get('group', default='').split(',')
            for group in groups:
                if group in self.groups:
                    self.groups[group].append(name)
                else:
                    self.groups[group] = [name]
        # since ''.split(',') == ['']
        # and groupless enums go there
        del self.groups['']

    def get_function_return_type(self, proto):
        # Everything up to the start of the name element is the return type.
        return self.all_text_until_element_name(proto, 'name').strip()

    def parse_function_definitions(self, reg):
        for command in reg.findall('commands/command'):
            proto = command.find('proto')
            name = proto.find('name').text
            ret_type = self.get_function_return_type(proto)

            func = GLFunction(ret_type, name)

            for arg in command.findall('param'):
                func.add_arg(self.all_text_until_element_name(arg, 'name').strip(),
                             arg.find('name').text, group = arg.get('group', default=None))

            alias = command.find('alias')
            if alias is not None:
                # Note that some alias references appear before the
                # target command is defined (glAttachObjectARB() ->
                # glAttachShader(), for example).
                func.alias_name = alias.get('name')

            self.functions[name] = func

    def drop_weird_glx_functions(self):
        # Drop a few ancient SGIX GLX extensions that use types not defined
        # anywhere in Xlib.  In glxext.h, they're protected by #ifdefs for the
        # headers that defined them.
        weird_functions = [name for name, func in self.functions.items()
                           if 'VLServer' in func.args_decl
                           or 'DMparams' in func.args_decl]

        for name in weird_functions:
            del self.functions[name]

    def resolve_aliases(self):
        for func in self.functions.values():
            # Find the root of the alias tree, and add ourselves to it.
            if func.alias_name != func.name:
                alias_func = func
                while alias_func.alias_name != alias_func.name:
                    alias_func = self.functions[alias_func.alias_name]
                func.alias_name = alias_func.name
                func.alias_func = alias_func
                alias_func.alias_exts.append(func)

    def prepare_provider_enum(self):
        self.provider_enum = {}

        # We assume that for any given provider, all functions using
        # it will have the same loader.  This lets us generate a
        # general C function for detecting conditions and calling the
        # dlsym/getprocaddress, and have our many resolver stubs just
        # call it with a table of values.
        for func in self.functions.values():
            for provider in func.providers.values():
                if provider.condition_name in self.provider_enum:
                    assert self.provider_condition[provider.condition_name] == provider.condition
                    assert self.provider_loader[provider.condition_name] == provider.loader
                    continue

                self.provider_enum[provider.condition_name] = provider.enum
                self.provider_condition[provider.condition_name] = provider.condition
                self.provider_loader[provider.condition_name] = provider.loader

    def sort_functions(self):
        self.sorted_functions = sorted(self.functions.values(), key=lambda func: func.name)

    def process_require_statements(self, feature, condition, loader, human_name):
        for command in feature.findall('require/command'):
            name = command.get('name')

            # wgl.xml describes 6 functions in WGL 1.0 that are in
            # gdi32.dll instead of opengl32.dll, and we would need to
            # change up our symbol loading to support that.  Just
            # don't wrap those functions.
            if self.target == 'wgl' and 'wgl' not in name:
                del self.functions[name]
                continue

            func = self.functions[name]
            func.add_provider(condition, loader, human_name)

    def parse_function_providers(self, reg):
        for feature in reg.findall('feature'):
            api = feature.get('api') # string gl, gles1, gles2, glx
            m = re.match(r'([0-9])\.([0-9])', feature.get('number'))
            version = int(m.group(1)) * 10 + int(m.group(2))

            self.supported_versions.add(feature.get('name'))

            if api == 'gl':
                human_name = 'Desktop OpenGL {0}'.format(feature.get('number'))
                condition = 'epoxy_is_desktop_gl()'

                loader = 'epoxy_get_core_proc_address({0}, {1})'.format('{0}', version)
                if version >= 11:
                    condition += ' && epoxy_conservative_gl_version() >= {0}'.format(version)
            elif api == 'gles2':
                human_name = 'OpenGL ES {0}'.format(feature.get('number'))
                condition = '!epoxy_is_desktop_gl() && epoxy_gl_version() >= {0}'.format(version)

                if version <= 20:
                    loader = 'epoxy_gles2_dlsym({0})'
                else:
                    loader = 'epoxy_gles3_dlsym({0})'
            elif api == 'gles1':
                human_name = 'OpenGL ES 1.0'
                condition = '!epoxy_is_desktop_gl() && epoxy_gl_version() >= 10 && epoxy_gl_version() < 20'
                loader = 'epoxy_gles1_dlsym({0})'
            elif api == 'glx':
                human_name = 'GLX {0}'.format(version)
                # We could just always use GPA for loading everything
                # but glXGetProcAddress(), but dlsym() is a more
                # efficient lookup.
                if version > 13:
                    condition = 'epoxy_conservative_glx_version() >= {0}'.format(version)
                    loader = 'glXGetProcAddress((const GLubyte *){0})'
                else:
                    condition = 'true'
                    loader = 'epoxy_glx_dlsym({0})'
            elif api == 'egl':
                human_name = 'EGL {0}'.format(version)
                if version > 10:
                    condition = 'epoxy_conservative_egl_version() >= {0}'.format(version)
                else:
                    condition = 'true'
                # All EGL core entrypoints must be dlsym()ed out --
                # eglGetProcAdddress() will return NULL.
                loader = 'epoxy_egl_dlsym({0})'
            elif api == 'wgl':
                human_name = 'WGL {0}'.format(version)
                condition = 'true'
                loader = 'epoxy_gl_dlsym({0})'
            elif api == 'glsc2':
                continue
            else:
                sys.exit('unknown API: "{0}"'.format(api))

            self.process_require_statements(feature, condition, loader, human_name)

        for extension in reg.findall('extensions/extension'):
            extname = extension.get('name')
            cond_extname = "enum_string[enum_string_offsets[i]]"

            self.supported_extensions.add(extname)

            # 'supported' is a set of strings like gl, gles1, gles2,
            # or glx, which are separated by '|'
            apis = extension.get('supported').split('|')
            if 'glx' in apis:
                condition = 'epoxy_conservative_has_glx_extension(provider_name)'
                loader = 'glXGetProcAddress((const GLubyte *){0})'
                self.process_require_statements(extension, condition, loader, extname)
            if 'egl' in apis:
                condition = 'epoxy_conservative_has_egl_extension(provider_name)'
                loader = 'eglGetProcAddress({0})'
                self.process_require_statements(extension, condition, loader, extname)
            if 'wgl' in apis:
                condition = 'epoxy_conservative_has_wgl_extension(provider_name)'
                loader = 'wglGetProcAddress({0})'
                self.process_require_statements(extension, condition, loader, extname)
            if {'gl', 'gles1', 'gles2'}.intersection(apis):
                condition = 'epoxy_conservative_has_gl_extension(provider_name)'
                loader = 'epoxy_get_proc_address({0})'
                self.process_require_statements(extension, condition, loader, extname)

    def fixup_bootstrap_function(self, name, loader):
        # We handle glGetString(), glGetIntegerv(), and
        # glXGetProcAddressARB() specially, because we need to use
        # them in the process of deciding on loaders for resolving,
        # and the naive code generation would result in their
        # resolvers calling their own resolvers.
        if name not in self.functions:
            return

        func = self.functions[name]
        func.providers = {}
        func.add_provider('true', loader, 'always present')

    def parse(self, xml_file):
        reg = ET.parse(xml_file)
        comment = reg.find('comment')
        if comment is not None:
            self.copyright_comment = comment.text
        else:
            self.copyright_comment = ''
        self.parse_typedefs(reg)
        self.parse_enums(reg)
        self.parse_function_definitions(reg)
        self.parse_function_providers(reg)

    def write_copyright_comment_body(self):
        for line in self.copyright_comment.splitlines():
            if '-----' in line:
                break
            self.outln(' * ' + line)

    def write_enums(self):
        for name in sorted(self.supported_versions):
            self.outln('#define {0} 1'.format(name))
        self.outln('')

        for name in sorted(self.supported_extensions):
            self.outln('#define {0} 1'.format(name))
        self.outln('')

        # We want to sort by enum number (which puts a bunch of things
        # in a logical order), then by name after that, so we do those
        # sorts in reverse.  This is still way uglier than doing some
        # sort based on what version/extensions things are introduced
        # in, but we haven't paid any attention to those attributes
        # for enums yet.
        sorted_by_name = sorted(self.enums.keys())
        sorted_by_number = sorted(sorted_by_name, key=lambda name: self.enums[name])
        for name in sorted_by_number:
            self.outln('#define ' + name.ljust(self.max_enum_name_len + 3) + self.enums[name] + '')

    def write_function_ptr_typedefs(self):
        for func in self.sorted_functions:
            self.outln('typedef {0} (GLAPIENTRY *{1})({2});'.format(func.ret_type,
                                                                    func.ptr_type,
                                                                    func.args_decl))

    def write_header_header(self, out_file):
        self.close()
        self.out_file = open(out_file, 'w')

        self.outln('/* GL dispatch header.')
        self.outln(' * This is code-generated from the GL API XML files from Khronos.')
        self.write_copyright_comment_body()
        self.outln(' */')
        self.outln('')

        self.outln('#pragma once')

        self.outln('#include <inttypes.h>')
        self.outln('#include <stddef.h>')
        self.outln('')

    def write_header(self, out_file):
        self.write_header_header(out_file)

        self.outln('#include "epoxy/common.h"')

        if self.target != "gl":
            self.outln('#include "epoxy/gl.h"')
            if self.target == "egl":
                self.outln('#include "EGL/eglplatform.h"')
                # Account for older eglplatform.h, which doesn't define
                # the EGL_CAST macro.
                self.outln('#ifndef EGL_CAST')
                self.outln('#if defined(__cplusplus)')
                self.outln('#define EGL_CAST(type, value) (static_cast<type>(value))')
                self.outln('#else')
                self.outln('#define EGL_CAST(type, value) ((type) (value))')
                self.outln('#endif')
                self.outln('#endif')
        else:
            # Add some ridiculous inttypes.h redefinitions that are
            # from khrplatform.h and not included in the XML.  We
            # don't directly include khrplatform.h because it's not
            # present on many systems, and coming up with #ifdefs to
            # decide when it's not present would be hard.
            self.outln('#define __khrplatform_h_ 1')
            self.outln('typedef int8_t khronos_int8_t;')
            self.outln('typedef int16_t khronos_int16_t;')
            self.outln('typedef int32_t khronos_int32_t;')
            self.outln('typedef int64_t khronos_int64_t;')
            self.outln('typedef uint8_t khronos_uint8_t;')
            self.outln('typedef uint16_t khronos_uint16_t;')
            self.outln('typedef uint32_t khronos_uint32_t;')
            self.outln('typedef uint64_t khronos_uint64_t;')
            self.outln('typedef float khronos_float_t;')
            self.outln('#ifdef _WIN64')
            self.outln('typedef signed   long long int khronos_intptr_t;')
            self.outln('typedef unsigned long long int khronos_uintptr_t;')
            self.outln('typedef signed   long long int khronos_ssize_t;')
            self.outln('typedef unsigned long long int khronos_usize_t;')
            self.outln('#else')
            self.outln('typedef signed   long int      khronos_intptr_t;')
            self.outln('typedef unsigned long int      khronos_uintptr_t;')
            self.outln('typedef signed   long int      khronos_ssize_t;')
            self.outln('typedef unsigned long int      khronos_usize_t;')
            self.outln('#endif')
            self.outln('typedef uint64_t khronos_utime_nanoseconds_t;')
            self.outln('typedef int64_t khronos_stime_nanoseconds_t;')
            self.outln('#define KHRONOS_MAX_ENUM 0x7FFFFFFF')
            self.outln('typedef enum {')
            self.outln('    KHRONOS_FALSE = 0,')
            self.outln('    KHRONOS_TRUE  = 1,')
            self.outln('    KHRONOS_BOOLEAN_ENUM_FORCE_SIZE = KHRONOS_MAX_ENUM')
            self.outln('} khronos_boolean_enum_t;')

        if self.target == "glx":
            self.outln('#include <X11/Xlib.h>')
            self.outln('#include <X11/Xutil.h>')

        self.out(self.typedefs_string)
        self.outln('')
        self.write_enums()
        self.outln('')
        self.write_function_ptr_typedefs()

        for func in self.sorted_functions:
            self.outln('EPOXY_PUBLIC {0} (EPOXY_CALLSPEC *epoxy_{1})({2});'.format(func.ret_type,
                                                                                   func.name,
                                                                                   func.args_decl))
            self.outln('')

        for func in self.sorted_functions:
            self.outln('#define {0} epoxy_{0}'.format(func.name))

    def write_function_ptr_resolver(self, func):
        self.outln('static {0}'.format(func.ptr_type))
        self.outln('epoxy_{0}_resolver(void)'.format(func.wrapped_name))
        self.outln('{')

        providers = []
        # Make a local list of all the providers for this alias group
        alias_root = func
        if func.alias_func:
            alias_root = func.alias_func
        for provider in alias_root.providers.values():
            providers.append(provider)
        for alias_func in alias_root.alias_exts:
            for provider in alias_func.providers.values():
                providers.append(provider)

        # Add some partial aliases of a few functions.  These are ones
        # that aren't quite aliases, because of some trivial behavior
        # difference (like whether to produce an error for a
        # non-Genned name), but where we'd like to fall back to the
        # similar function if the proper one isn't present.
        half_aliases = {
            'glBindVertexArray' : 'glBindVertexArrayAPPLE',
            'glBindVertexArrayAPPLE' : 'glBindVertexArray',
            'glBindFramebuffer' : 'glBindFramebufferEXT',
            'glBindFramebufferEXT' : 'glBindFramebuffer',
            'glBindRenderbuffer' : 'glBindRenderbufferEXT',
            'glBindRenderbufferEXT' : 'glBindRenderbuffer',
        }
        if func.name in half_aliases:
            alias_func = self.functions[half_aliases[func.name]]
            for provider in alias_func.providers.values():
                providers.append(provider)

        def provider_sort(provider):
            return (provider.name != func.name, provider.name, provider.enum)
        providers.sort(key=provider_sort)

        if len(providers) != 1:
            self.outln('    static const enum {0}_provider providers[] = {{'.format(self.target))
            for provider in providers:
                self.outln('        {0},'.format(provider.enum))
            self.outln('        {0}_provider_terminator'.format(self.target))
            self.outln('    };')

            self.outln('    static const uint32_t entrypoints[] = {')
            if len(providers) > 1:
                for provider in providers:
                    self.outln('        {0} /* "{1}" */,'.format(self.entrypoint_string_offset[provider.name], provider.name))
            else:
                self.outln('        0 /* None */,')
            self.outln('    };')

            self.outln('    return {0}_provider_resolver(entrypoint_strings + {1} /* "{2}" */,'.format(self.target,
                                                                                                       self.entrypoint_string_offset[func.name],
                                                                                                       func.name))
            self.outln('                                providers, entrypoints);')
        else:
            assert providers[0].name == func.name
            self.outln('    return {0}_single_resolver({1}, {2} /* {3} */);'.format(self.target,
                                                                                    providers[0].enum,
                                                                                    self.entrypoint_string_offset[func.name],
                                                                                    func.name))
        self.outln('}')
        self.outln('')

    def write_thunks(self, func):
        # Writes out the function that's initially plugged into the
        # global function pointer, which resolves, updates the global
        # function pointer, and calls down to it.
        #
        # It also writes out the actual initialized global function
        # pointer.
        if func.ret_type == 'void':
            self.outln('GEN_THUNKS({0}, ({1}), ({2}))'.format(func.wrapped_name,
                                                              func.args_decl,
                                                              func.args_list))
        else:
            self.outln('GEN_THUNKS_RET({0}, {1}, ({2}), ({3}))'.format(func.ret_type,
                                                                       func.wrapped_name,
                                                                       func.args_decl,
                                                                       func.args_list))

    def write_function_pointer(self, func):
        self.outln('{0} epoxy_{1} = epoxy_{1}_global_rewrite_ptr;'.format(func.ptr_type, func.wrapped_name))
        self.outln('')

    def write_provider_enums(self):
        # Writes the enum declaration for the list of providers
        # supported by gl_provider_resolver()

        self.outln('')
        self.outln('enum {0}_provider {{'.format(self.target))

        sorted_providers = sorted(self.provider_enum.keys())

        # We always put a 0 enum first so that we can have a
        # terminator in our arrays
        self.outln('    {0}_provider_terminator = 0,'.format(self.target))

        for human_name in sorted_providers:
            enum = self.provider_enum[human_name]
            self.outln('    {0},'.format(enum))
        self.outln('} PACKED;')
        self.outln('ENDPACKED')
        self.outln('')

    def write_provider_enum_strings(self):
        # Writes the mapping from enums to the strings describing them
        # for epoxy_print_failure_reasons().

        sorted_providers = sorted(self.provider_enum.keys())

        offset = 0
        self.outln('static const char *enum_string =')
        for human_name in sorted_providers:
            self.outln('    "{0}\\0"'.format(human_name))
            self.enum_string_offset[human_name] = offset
            offset += len(human_name.replace('\\', '')) + 1
        self.outln('     ;')
        self.outln('')
        # We're using uint16_t for the offsets.
        assert offset < 65536

        self.outln('static const uint16_t enum_string_offsets[] = {')
        self.outln('    -1, /* {0}_provider_terminator, unused */'.format(self.target))
        for human_name in sorted_providers:
            enum = self.provider_enum[human_name]
            self.outln('    {1}, /* {0} */'.format(human_name, self.enum_string_offset[human_name]))
        self.outln('};')
        self.outln('')

    def write_entrypoint_strings(self):
        self.outln('static const char entrypoint_strings[] = {')
        offset = 0
        for func in self.sorted_functions:
            if func.name not in self.entrypoint_string_offset:
                self.entrypoint_string_offset[func.name] = offset
                offset += len(func.name) + 1
                for c in func.name:
                    self.outln("   '{0}',".format(c))
                self.outln('   0, // {0}'.format(func.name))
        self.outln('    0 };')
        # We're using uint16_t for the offsets.
        #assert(offset < 65536)
        self.outln('')

    def write_provider_resolver(self):
        self.outln('static void *{0}_provider_resolver(const char *name,'.format(self.target))
        self.outln('                                   const enum {0}_provider *providers,'.format(self.target))
        self.outln('                                   const uint32_t *entrypoints)')
        self.outln('{')
        self.outln('    int i;')

        self.outln('    for (i = 0; providers[i] != {0}_provider_terminator; i++) {{'.format(self.target))
        self.outln('        const char *provider_name = enum_string + enum_string_offsets[providers[i]];')
        self.outln('        switch (providers[i]) {')
        self.outln('')

        for human_name in sorted(self.provider_enum.keys()):
            enum = self.provider_enum[human_name]
            self.outln('        case {0}:'.format(enum))
            self.outln('            if ({0})'.format(self.provider_condition[human_name]))
            self.outln('                return {0};'.format(self.provider_loader[human_name]).format("entrypoint_strings + entrypoints[i]"))
            self.outln('            break;')

        self.outln('        case {0}_provider_terminator:'.format(self.target))
        self.outln('            abort(); /* Not reached */')
        self.outln('        }')
        self.outln('    }')
        self.outln('')

        self.outln('    if (epoxy_resolver_failure_handler)')
        self.outln('        return epoxy_resolver_failure_handler(name);')
        self.outln('')

        # If the function isn't provided by any known extension, print
        # something useful for the poor application developer before
        # aborting.  (In non-epoxy GL usage, the app developer would
        # call into some blank stub function and segfault).
        self.outln('    fprintf(stderr, "No provider of %s found.  Requires one of:\\n", name);')
        self.outln('    for (i = 0; providers[i] != {0}_provider_terminator; i++) {{'.format(self.target))
        self.outln('        fprintf(stderr, "    %s\\n", enum_string + enum_string_offsets[providers[i]]);')
        self.outln('    }')
        self.outln('    if (providers[0] == {0}_provider_terminator) {{'.format(self.target))
        self.outln('        fprintf(stderr, "    No known providers.  This is likely a bug "')
        self.outln('                        "in libepoxy code generation\\n");')
        self.outln('    }')
        self.outln('    abort();')

        self.outln('}')
        self.outln('')

        single_resolver_proto = '{0}_single_resolver(enum {0}_provider provider, uint32_t entrypoint_offset)'.format(self.target)
        self.outln('EPOXY_NOINLINE static void *')
        self.outln('{0};'.format(single_resolver_proto))
        self.outln('')
        self.outln('static void *')
        self.outln('{0}'.format(single_resolver_proto))
        self.outln('{')
        self.outln('    enum {0}_provider providers[] = {{'.format(self.target))
        self.outln('        provider,')
        self.outln('        {0}_provider_terminator'.format(self.target))
        self.outln('    };')
        self.outln('    return {0}_provider_resolver(entrypoint_strings + entrypoint_offset,'.format(self.target))
        self.outln('                                providers, &entrypoint_offset);')
        self.outln('}')
        self.outln('')

    def write_source(self, f):
        self.close()
        self.out_file = open(f, 'w')

        self.outln('/* GL dispatch code.')
        self.outln(' * This is code-generated from the GL API XML files from Khronos.')
        self.write_copyright_comment_body()
        self.outln(' */')
        self.outln('')
        self.outln('#include "config.h"')
        self.outln('')
        self.outln('#include <stdlib.h>')
        self.outln('#include <string.h>')
        self.outln('#include <stdio.h>')
        self.outln('')
        self.outln('#include "dispatch_common.h"')
        self.outln('#include "epoxy/{0}.h"'.format(self.target))
        self.outln('')
        self.outln('#ifdef __GNUC__')
        self.outln('#define EPOXY_NOINLINE __attribute__((noinline))')
        self.outln('#elif defined (_MSC_VER)')
        self.outln('#define EPOXY_NOINLINE __declspec(noinline)')
        self.outln('#endif')

        self.outln('struct dispatch_table {')
        for func in self.sorted_functions:
            self.outln('    {0} epoxy_{1};'.format(func.ptr_type, func.wrapped_name))
        self.outln('};')
        self.outln('')

        # Early declaration, so we can declare the real thing at the
        # bottom. (I want the function_ptr_resolver as the first
        # per-GL-call code, since it's the most interesting to see
        # when you search for the implementation of a call)
        self.outln('#if USING_DISPATCH_TABLE')
        self.outln('static inline struct dispatch_table *')
        self.outln('get_dispatch_table(void);')
        self.outln('')
        self.outln('#endif')

        self.write_provider_enums()
        self.write_provider_enum_strings()
        self.write_entrypoint_strings()
        self.write_provider_resolver()

        for func in self.sorted_functions:
            self.write_function_ptr_resolver(func)

        for func in self.sorted_functions:
            self.write_thunks(func)
        self.outln('')

        self.outln('#if USING_DISPATCH_TABLE')

        self.outln('static struct dispatch_table resolver_table = {')
        for func in self.sorted_functions:
            self.outln('    epoxy_{0}_dispatch_table_rewrite_ptr, /* {0} */'.format(func.wrapped_name))
        self.outln('};')
        self.outln('')

        self.outln('uint32_t {0}_tls_index;'.format(self.target))
        self.outln('uint32_t {0}_tls_size = sizeof(struct dispatch_table);'.format(self.target))
        self.outln('')

        self.outln('static inline struct dispatch_table *')
        self.outln('get_dispatch_table(void)')
        self.outln('{')
        self.outln('	return TlsGetValue({0}_tls_index);'.format(self.target))
        self.outln('}')
        self.outln('')

        self.outln('void')
        self.outln('{0}_init_dispatch_table(void)'.format(self.target))
        self.outln('{')
        self.outln('    struct dispatch_table *dispatch_table = get_dispatch_table();')
        self.outln('    memcpy(dispatch_table, &resolver_table, sizeof(resolver_table));')
        self.outln('}')
        self.outln('')

        self.outln('void')
        self.outln('{0}_switch_to_dispatch_table(void)'.format(self.target))
        self.outln('{')

        for func in self.sorted_functions:
            self.outln('    epoxy_{0} = epoxy_{0}_dispatch_table_thunk;'.format(func.wrapped_name))

        self.outln('}')
        self.outln('')

        self.outln('#endif /* !USING_DISPATCH_TABLE */')

        for func in self.sorted_functions:
            self.write_function_pointer(func)

    def write_vapi(self, out_file):
        vapi_fix_ret_type = lambda x: x.replace('const ', '')
        vapi_extract_ctype = lambda x: x[8:].replace('unsigned ', 'u').replace(' (', '')
        vapi_remove_invalid_args = lambda x: '' if x == 'void' else \
            x.replace('(void)','()').replace('const ', '').replace('const*', '*') \
                .replace('struct _cl_', '_cl_')
        def vapi_func_args_decl(func):
            validify = lambda x: x.replace('const ', '').replace('const*', '*').replace('struct _cl_', '_cl_')
            result = ', '.join(list(map(
                    lambda arg: (arg.get('group') or validify(arg['type'])) + ' ' + arg['name'],
                    func.args
                )))
            return result
        self.close()
        self.out_file = open(out_file, 'w')

        self.outln('/* VAPI for libepoxy GL dispatch header')
        self.outln(' * This is code-generated from the GL API XML files from Khronos.')
        self.write_copyright_comment_body()
        self.outln(' */')
        self.outln('')

        self.outln('[CCode (cheader_filename = "epoxy/gl.h")]')
        self.outln('namespace GL {')

        self.outln('\t// These types should be defined in `khrplatform.h`')
        self.outln('\t// but libepoxy defines them explicitly instead of including.')
        self.outln('\t// Names kept in C-style for easier inheritance handling.')
        self.outln('\t[SimpleType] public struct khronos_int8_t : int8 {}')
        self.outln('\t[SimpleType] public struct khronos_int16_t : int16 {}')
        self.outln('\t[SimpleType] public struct khronos_int32_t : int32 {}')
        self.outln('\t[SimpleType] public struct khronos_int64_t : int64 {}')
        self.outln('\t[SimpleType] public struct khronos_uint8_t : uint8 {}')
        self.outln('\t[SimpleType] public struct khronos_uint16_t : uint16 {}')
        self.outln('\t[SimpleType] public struct khronos_uint32_t : uint32 {}')
        self.outln('\t[SimpleType] public struct khronos_uint64_t : uint64 {}')
        self.outln('\t[SimpleType] public struct khronos_float_t : float {}')
        self.outln('\t[SimpleType] public struct khronos_intptr_t  : long {}')
        self.outln('\t[SimpleType] public struct khronos_uintptr_t : ulong {}')
        ### From https://developer-old.gnome.org/glib/stable/glib-Basic-Types.html#gint64
        ## typedef signed long gint64;
        ## From https://github.com/KhronosGroup/EGL-Registry/blob/main/api/KHR/khrplatform.h
        ## typedef signed   long  int     khronos_ssize_t;
        ## So `gint64` and `khronos_ssize_t` should be identical
        ##
        ## But vala's `sizeof()` returns `ulong`
        ## which cannot be implicitly converted to `khronos_ssize_t`
        ## So I decided to define it as `gint64` in order to allow implicit conversion
        ## `khronos_usize_t` fits for `sizeof()` so expanding it isn't necesary
        self.outln('\t// XXX: Actually it\'s `long` Look into `src/gen_dispatch.py` for explanation')
        self.outln('\t[SimpleType] public struct khronos_ssize_t : int64 {}')
        self.outln('\t[SimpleType] public struct khronos_usize_t : ulong {}')
        self.outln('\t[SimpleType] public struct khronos_utime_nanoseconds_t : uint64 {}')
        self.outln('\t[SimpleType] public struct khronos_stime_nanoseconds_t : int64 {}')
        self.outln('\tpublic const int KHRONOS_MAX_ENUM;')
        self.outln('\tpublic enum khronos_boolean_enum_t {')
        self.outln('\t    [CCode(cname = "KHRONOS_FALSE")] FALSE,')
        self.outln('\t    [CCode(cname = "KHRONOS_TRUE")] TRUE,')
        self.outln('\t    [CCode(cname = "KHRONOS_BOOLEAN_ENUM_FORCE_SIZE")] FORCE_SIZE,')
        self.outln('\t}')
        self.outln('')

        # Types that are unique and writed separately
        self.outln('\t[CCode (cname = "{0}", cprefix = "{1}")]'.format('GLsync', ''))
        self.outln('\t[Compact]')
        self.outln('\tpublic class GLsync {\n\t}')
        self.outln('\t[CCode (cname = "{0}", cprefix = "{1}")]'.format('GLhandleARB', ''))
        self.outln('\t[SimpleType]')
        self.outln('\tpublic struct {0} : {1} {{\n\t}}'.format('GLhandleARB', 'uint'))

        self.outln('\t[CCode (cname = "{0}", cprefix = "{1}")]'.format('struct _cl_context', ''))
        self.outln('\t[SimpleType]')
        self.outln('\tpublic struct {0} {{\n\t}}'.format('_cl_context', ''))
        self.outln('\t[CCode (cname = "{0}", cprefix = "{1}")]'.format('_cl_event', ''))
        self.outln('\t[SimpleType]')
        self.outln('\tpublic struct {0} {{\n\t}}'.format('_cl_event', ''))

        for typedef in self.typedefs:
            if not typedef.is_apientry and typedef.name not in {'', 'GLhandleARB', 'GLsync'} and typedef.prefix not in {''}:
                # void is not valid type to inherit in vala and void* types meant to be classes
                if vapi_extract_ctype(typedef.prefix) == 'void *':
                    self.outln('\t[CCode (cname = "{0}", cprefix = "{1}")]'.format(typedef.name, ''))
                    self.outln('\t[Compact]')
                    self.outln('\tpublic class {0} {{\n\t}}'.format(
                            typedef.name
                        ))
                elif vapi_extract_ctype(typedef.prefix) == 'void ':
                    self.outln('\t[CCode (cname = "{0}", cprefix = "{1}")]'.format(typedef.name, ''))
                    self.outln('\t[SimpleType]')
                    self.outln('\tpublic struct {0} {{\n\t}}'.format(
                            typedef.name
                        ))
                else:
                    self.outln('\t[CCode (cname = "{0}", cprefix = "{1}")]'.format(typedef.name, ''))
                    self.outln('\t[SimpleType]')
                    self.outln('\tpublic struct {0} : {1} {{\n\t}}'.format(
                            typedef.name,
                            vapi_extract_ctype(typedef.prefix)
                        ))
            elif typedef.is_apientry:
                self.outln('\t[CCode (cname = "{0}", cprefix = "{1}", has_target="false")]'.format(typedef.name, ''))
                self.outln('\tpublic delegate {0} {1} {2}'.format(
                        vapi_extract_ctype(typedef.prefix),
                        typedef.name,
                        vapi_remove_invalid_args(typedef.postfix[1:])
                    ))
        self.outln('')

        self.outln('// since vala has no `#ifdef` such defines are useless ')
        self.outln('// but we keep them for API compatibility with C code')
        for version in sorted(self.supported_versions):
            self.outln('\tpublic const int {0};'.format(version))
        for ext in sorted(self.supported_extensions):
            self.outln('\tpublic const int {0};'.format(ext))
        self.outln('')

        self.outln('// Elements name for vala are subject to change, (i would like make them bit shorter)')
        self.outln('// Sadly it\'s hard to differ usual enums from bitfields to set [Flags] attribute')
        for group, elems in self.groups.items():
            ## Prefix could be enum name but there is edge cases
            self.outln('\t[CCode (cname = "int", cprefix = "{0}", has_type_id = false)]'.format(""))
            self.outln('\tpublic enum {0} {{'.format(group))
            for elem in elems:
                self.outln('\t\t [CCode (cname = "{0}")] {1},'.format(elem, elem))
            self.outln('\t}')
        self.outln('')

        # In C it is 'typedef {0} (GLAPIENTRY *{1})({2});'.format(func.ret_type, func.ptr_type, func.args_decl)
        for func in self.sorted_functions:
            self.outln('\t[CCode (cname = "{0}", cprefix = "{1}", has_target = "false")]'.format(func.ptr_type, ''))
            self.outln('\tpublic delegate {0} {1}({2});'.format(
                    vapi_fix_ret_type(func.ret_type),
                    func.ptr_type,
                    vapi_remove_invalid_args(func.args_decl)
                ))
        self.outln('')

        self.outln('\t// Huge workaround cause vala\'s sizeof() returns ulong')
        self.outln('\t// Should be called with constants instead of types')
        self.outln('\t[CCode (cname = "sizeof", simple_generics = true)]')
        self.outln('\tGLsizei glSizeof<T>(T x);')
        self.outln('')

        for func in self.sorted_functions:
            self.outln('\t[CCode (cname = "{0}", cprefix = "{1}")]'.format(func.name, ''))
            self.outln('\tpublic {0} epoxy_{1}({2});'.format(
                    vapi_fix_ret_type(func.ret_type),
                    func.name,
                    vapi_remove_invalid_args(func.args_decl)
                ))
        self.outln('')

        for func in self.sorted_functions:
            self.outln('\t[CCode (cname = "{0}", cprefix = "{1}")]'.format(func.name, ''))
            self.outln('\tpublic {0} {1}({2});'.format(
                    vapi_fix_ret_type(func.ret_type),
                    func.name,
                    vapi_func_args_decl(func)
                ))

        self.outln('}')

        self.close()

    def close(self):
        if self.out_file:
            self.out_file.close()
            self.out_file = None


argparser = argparse.ArgumentParser(description='Generate GL dispatch wrappers.')
argparser.add_argument('files', metavar='file.xml', nargs='+', help='GL API XML files to be parsed')
argparser.add_argument('--outputdir', metavar='dir', required=False, help='Destination directory for files (default to current dir)')
argparser.add_argument('--includedir', metavar='dir', required=False, help='Destination directory for headers')
argparser.add_argument('--srcdir', metavar='dir', required=False, help='Destination directory for source')
argparser.add_argument('--source', dest='source', action='store_true', required=False, help='Generate the source file')
argparser.add_argument('--no-source', dest='source', action='store_false', required=False, help='Do not generate the source file')
argparser.add_argument('--header', dest='header', action='store_true', required=False, help='Generate the header file')
argparser.add_argument('--no-header', dest='header', action='store_false', required=False, help='Do not generate the header file')
argparser.add_argument('--vapi', dest='vapi', action='store_true', required=False, help='Generate the vapi file')
argparser.add_argument('--no-vapi', dest='vapi', action='store_false', required=False, help='Do not generate the vapi file')
args = argparser.parse_args()

if args.outputdir:
    outputdir = args.outputdir
else:
    outputdir = os.getcwd()

if args.includedir:
    includedir = args.includedir
else:
    includedir = outputdir

if args.srcdir:
    srcdir = args.srcdir
else:
    srcdir = outputdir

build_source = args.source
build_header = args.header
build_vapi = args.vapi

if not build_source and not build_header and not build_vapi:
    build_source = True
    build_header = True

for f in args.files:
    name = os.path.basename(f).split('.xml')[0]
    generator = Generator(name)
    generator.parse(f)

    generator.drop_weird_glx_functions()

    # This is an ANSI vs Unicode function, handled specially by
    # include/epoxy/wgl.h
    if 'wglUseFontBitmaps' in generator.functions:
        del generator.functions['wglUseFontBitmaps']

    generator.sort_functions()
    generator.resolve_aliases()
    generator.fixup_bootstrap_function('glGetString',
                                       'epoxy_get_bootstrap_proc_address({0})')
    generator.fixup_bootstrap_function('glGetIntegerv',
                                       'epoxy_get_bootstrap_proc_address({0})')

    # While this is technically exposed as a GLX extension, it's
    # required to be present as a public symbol by the Linux OpenGL
    # ABI.
    generator.fixup_bootstrap_function('glXGetProcAddress',
                                       'epoxy_glx_dlsym({0})')

    generator.prepare_provider_enum()

    if build_header:
        generator.write_header(os.path.join(includedir, name + '_generated.h'))
    if build_source:
        generator.write_source(os.path.join(srcdir, name + '_generated_dispatch.c'))

    if build_vapi:
        generator.write_vapi(os.path.join(includedir, name + '_generated.vapi'))

    generator.close()
