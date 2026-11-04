"""The CR sandbox's binderless guards for the state-manager pump (jni_bridge.cpp nativePumpManager, active code only, default
switches), described as patches (patch.py) so they are assembled for whichever build is loaded; addresses come from bindings.

What they do (all presentation / Android-service paths, none in battle or data-table logic):
  sc3d_context, sc3d_capability, sc3d_feature   skip work when the 3D renderer's context/capability/feature object is missing
  ui_locale_container                           skip a UI locale registration when its container is missing
  loading_screen_init                           the loading screen's setup returns at once
  invalid_asset, metadata_validity, external_asset   asset checks that need Android's packaging take their "valid" path
  home_graphics_create                          the home screen's graphics creation call is skipped
Around every update (undone after): the presentation toggle returns at once and the home screen's resolution block uses a fixed
2048 x 2048.

The UI locale registration calls through the engine's obfuscation dispatcher (6,529 call sites in 150535029), which picks the real
function by the RETURN ADDRESS; its stub pushes the original return address and jumps, so the dispatcher sees the original call.
Any guard that moves a call to the dispatcher must do the same.
"""
import patch as P

PERMANENT = {
    'sc3d_context': P.Patch('sc3d_context_site', 2, [
        ('copy', 0), ('raw', '4885c0'), ('je', 'sc3d_context_skip'), ('copy', 1), ('back', 2)], stub=True),
    'sc3d_capability': P.Patch('sc3d_capability_site', 2, [
        ('copy', 0), ('raw', '4885ff'), ('je', 'sc3d_capability_skip'), ('copy', 1), ('back', 2)], stub=True),
    'sc3d_feature': P.Patch('sc3d_feature_site', 2, [
        ('raw', '4d85ff'), ('je', 'sc3d_feature_skip'), ('copy', 0), ('copy', 1), ('back', 2)], stub=True),
    'ui_locale_container': P.Patch('ui_locale_call', 1, [
        ('raw', '4885ff'), ('je', '.skip'), ('raw', '48833f00'), ('je', '.skip'),       # container null or empty -> skip
        ('push_back', 1), ('jmp', '@0'),                                                  # the call, as if from its own site
        ('label', '.skip'), ('back', 1)], stub=True),
    'loading_screen_init': P.ret('loading_screen_init'),
    'invalid_asset': P.Patch('invalid_asset_site', 1, [
        ('raw', '488d7c2410'), ('call', 'asset_check'), ('raw', '4531ff'), ('jmp', 'invalid_asset_resume')], stub=True),
    'metadata_validity': P.Patch('metadata_validity_site', 3, [
        ('raw', '84c0'), ('je', 'metadata_invalid'), ('raw', '4c8d742410'), ('jmp', 'metadata_valid_resume')], stub=True),
    'external_asset': P.Patch('external_asset_site', 1, [
        ('raw', '4889df'), ('call', 'external_asset_check'), ('raw', '84c0'), ('je', 'external_asset_failure'),
        ('copy', 0), ('back', 1)], stub=True),
    'home_graphics_create': P.nop('home_graphics_create_call'),
}
AROUND_UPDATE = [
    P.ret('presentation_toggle'),
    P.Patch('home_resolution_site', 3, [('raw', '41be00080000'), ('raw', '41bf00080000'),       # r14d = r15d = 2048
                                        ('jmp', 'home_resolution_resume')], stub=False),
]


def install(g) -> list:
    """Every permanent guard (each checks its bytes first; a different build is refused)."""
    for name, p in PERMANENT.items():
        p.apply(g)
    return list(PERMANENT)


def around_update(g, update):
    records = [p.apply(g) for p in AROUND_UPDATE]
    try:
        return update()
    finally:
        for record in reversed(records):
            P.undo(record)
