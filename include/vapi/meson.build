# GL is always generated
generated_vapi = [ [ 'gl.vapi', 'gl.h', gl_registry ] ]
generated_vapi_registry = [ gl_registry ]

if build_egl
  generated_vapi += [ [ 'egl.vapi', 'egl.h', egl_registry ] ]
endif

if build_glx
  generated_vapi += [ [ 'glx.vapi', 'glx.h', glx_registry ] ]
endif

if build_wgl
  generated_vapi += [ [ 'wgl.vapi', 'wgl.h', wgl_registry ] ]
endif

gen_vapis = []

foreach g: generated_vapi
  vapi = g[0]
  registry = g[2]
  generated = custom_target(vapi,
                            input: registry,
                            output: [ vapi ],
                            command: [
                              gen_dispatch_py,
                              '--no-header',
                              '--no-source',
                              '--vapi',
                              '--outputdir=@OUTDIR@',
                              '@INPUT@',
                            ],
                            install: true,
                            install_dir: epoxy_vapidir)

  gen_vapis += [ generated ]
endforeach

epoxy_vapis = gen_vapis
epoxy_vapidir = meson.current_build_dir()
