import os

from buildbot.scheduler import Scheduler, Dependent, Triggerable
from buildbot.status.tinderbox import TinderboxMailNotifier
from buildbot.process.factory import BuildFactory

from buildbotcustom.l10n import DependentL10n
from buildbotcustom.misc import get_l10n_repositories, isHgPollerTriggered, \
  generateTestBuilderNames, generateTestBuilder, _nextFastSlave
from buildbotcustom.process.factory import StagingRepositorySetupFactory, \
  ReleaseTaggingFactory, SingleSourceFactory, ReleaseBuildFactory, \
  ReleaseUpdatesFactory, UpdateVerifyFactory, ReleaseFinalVerification, \
  L10nVerifyFactory, ReleaseRepackFactory, \
  PartnerRepackFactory, MajorUpdateFactory, XulrunnerReleaseBuildFactory, \
  TuxedoEntrySubmitterFactory, makeDummyBuilder
from buildbotcustom.changes.ftppoller import FtpPoller

def generateReleaseBranchObjects(releaseConfig, branchConfig, staging):
    def builderPrefix(s, platform=None):
        if platform:
            return "release-%s-%s_%s" % (releaseConfig['sourceRepoName'], platform, s)
        else:
            return "release-%s-%s" % (releaseConfig['sourceRepoName'], s)

    builders = []
    test_builders = []
    schedulers = []
    change_source = []
    status = []

    ##### Change sources and Schedulers
    change_source.append(FtpPoller(
        branch=builderPrefix("post_signing"),
        ftpURLs=[
            "http://%s/pub/mozilla.org/%s/nightly/%s-candidates/build%s/" % (
                   releaseConfig['stagingServer'],
                   releaseConfig['productName'], releaseConfig['version'],
                   releaseConfig['buildNumber'])],
        pollInterval= 60*10,
        searchString='win32_signing_build'
    ))

    if staging:
        repo_setup_scheduler = Scheduler(
            name=builderPrefix('repo_setup'),
            branch=releaseConfig['sourceRepoPath'],
            treeStableTimer=None,
            builderNames=[builderPrefix('repo_setup')],
            fileIsImportant=lambda c: not isHgPollerTriggered(c,
                branchConfig['hgurl'])
        )
        schedulers.append(repo_setup_scheduler)
        tag_scheduler = Dependent(
            name=builderPrefix('tag'),
            upstream=repo_setup_scheduler,
            builderNames=[builderPrefix('tag')]
        )
    else:
        tag_scheduler = Scheduler(
            name='tag',
            branch=releaseConfig['sourceRepoPath'],
            treeStableTimer=None,
            builderNames=[builderPrefix('tag')],
            fileIsImportant=lambda c: not isHgPollerTriggered(c, branchConfig['hgurl'])
        )

    schedulers.append(tag_scheduler)
    source_scheduler = Dependent(
        name=builderPrefix('source'),
        upstream=tag_scheduler,
        builderNames=[builderPrefix('source')]
    )
    schedulers.append(source_scheduler)

    if releaseConfig['xulrunnerPlatforms']:
        xulrunner_source_scheduler = Dependent(
            name=builderPrefix('xulrunner_source'),
            upstream=tag_scheduler,
            builderNames=[builderPrefix('xulrunner_source')]
        )
        schedulers.append(xulrunner_source_scheduler)

    for platform in releaseConfig['enUSPlatforms']:
        build_scheduler = Dependent(
            name=builderPrefix('%s_build' % platform),
            upstream=tag_scheduler,
            builderNames=[builderPrefix('%s_build' % platform)]
        )
        schedulers.append(build_scheduler)
        if platform in releaseConfig['l10nPlatforms']:
            repack_scheduler = DependentL10n(
                name=builderPrefix('%s_repack' % platform),
                platform=platform,
                upstream=build_scheduler,
                builderNames=[builderPrefix('%s_repack' % platform)],
                branch=releaseConfig['sourceRepoPath'],
                baseTag='%s_RELEASE' % releaseConfig['baseTag'],
                localesFile='browser/locales/shipped-locales',
            )
            schedulers.append(repack_scheduler)

    for platform in releaseConfig['xulrunnerPlatforms']:
        xulrunner_build_scheduler = Dependent(
            name=builderPrefix('xulrunner_%s_build' % platform),
            upstream=tag_scheduler,
            builderNames=[builderPrefix('xulrunner_%s_build' % platform)]
        )
        schedulers.append(xulrunner_build_scheduler)

    if releaseConfig['doPartnerRepacks']:
        partner_scheduler = Scheduler(
            name=builderPrefix('partner_repacks'),
            treeStableTimer=0,
            branch=builderPrefix('post_signing'),
            builderNames=[builderPrefix('partner_repack')],
        )
        schedulers.append(partner_scheduler)
    for platform in releaseConfig['l10nPlatforms']:
        l10n_verify_scheduler = Scheduler(
            name=builderPrefix('l10n_verification', platform),
            treeStableTimer=0,
            branch=builderPrefix('post_signing'),
            builderNames=[builderPrefix('l10n_verification', platform)]
        )
        schedulers.append(l10n_verify_scheduler)

    updates_scheduler = Scheduler(
        name=builderPrefix('updates'),
        treeStableTimer=0,
        branch=builderPrefix('post_signing'),
        builderNames=[builderPrefix('updates')]
    )
    schedulers.append(updates_scheduler)

    updateBuilderNames = []
    for platform in sorted(releaseConfig['verifyConfigs'].keys()):
        updateBuilderNames.append(builderPrefix('%s_update_verify' % platform))
    update_verify_scheduler = Dependent(
        name=builderPrefix('update_verify'),
        upstream=updates_scheduler,
        builderNames=updateBuilderNames
    )
    schedulers.append(update_verify_scheduler)

    if releaseConfig['majorUpdateRepoPath']:
        majorUpdateBuilderNames = []
        for platform in sorted(releaseConfig['majorUpdateVerifyConfigs'].keys()):
            majorUpdateBuilderNames.append(
                    builderPrefix('%s_major_update_verify' % platform))
        major_update_verify_scheduler = Triggerable(
            name=builderPrefix('major_update_verify'),
            builderNames=majorUpdateBuilderNames
        )
        schedulers.append(major_update_verify_scheduler)

    for platform in releaseConfig['unittestPlatforms']:
        platform_test_builders = []
        base_name = branchConfig['platforms'][platform]['base_name']
        for suites_name, suites in branchConfig['unittest_suites']:
            platform_test_builders.extend(
                    generateTestBuilderNames(
                        builderPrefix('%s_test' % platform),
                        suites_name, suites))

        s = Scheduler(
         name=builderPrefix('%s-opt-unittest' % platform),
         treeStableTimer=0,
         branch=builderPrefix('%s-opt-unittest' % platform),
         builderNames=platform_test_builders,
        )
        schedulers.append(s)

    # Purposely, there is not a Scheduler for ReleaseFinalVerification
    # This is a step run very shortly before release, and is triggered manually
    # from the waterfall

    ##### Builders
    clone_repositories = {
        releaseConfig['sourceRepoClonePath']: {
            'revision': releaseConfig['sourceRepoRevision'],
            'relbranchOverride': releaseConfig['relbranchOverride'],
            'bumpFiles': ['config/milestone.txt', 'js/src/config/milestone.txt',
                          'browser/config/version.txt']
        }
    }
    if len(releaseConfig['l10nPlatforms']) > 0:
        l10n_clone_repos = get_l10n_repositories(releaseConfig['l10nRevisionFile'],
                                                 releaseConfig['l10nRepoClonePath'],
                                                 releaseConfig['relbranchOverride'])
        clone_repositories.update(l10n_clone_repos)

    tag_repositories = {
        releaseConfig['sourceRepoPath']: {
            'revision': releaseConfig['sourceRepoRevision'],
            'relbranchOverride': releaseConfig['relbranchOverride'],
            'bumpFiles': ['config/milestone.txt', 'js/src/config/milestone.txt',
                          'browser/config/version.txt']
        }
    }
    if len(releaseConfig['l10nPlatforms']) > 0:
        l10n_tag_repos = get_l10n_repositories(releaseConfig['l10nRevisionFile'],
                                               releaseConfig['l10nRepoPath'],
                                               releaseConfig['relbranchOverride'])
        tag_repositories.update(l10n_tag_repos)


    if staging:
        if not releaseConfig.get('skip_repo_setup'):
            repository_setup_factory = StagingRepositorySetupFactory(
                hgHost=branchConfig['hghost'],
                buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                username=releaseConfig['hgUsername'],
                sshKey=releaseConfig['hgSshKey'],
                repositories=clone_repositories,
                clobberURL=branchConfig['base_clobber_url'],
            )

            builders.append({
                'name': builderPrefix('repo_setup'),
                'slavenames': branchConfig['platforms']['linux']['slaves'],
                'category': 'release',
                'builddir': builderPrefix('repo_setup'),
                'factory': repository_setup_factory,
                'nextSlave': _nextFastSlave,
            })
        else:
            builders.append(makeDummyBuilder(
                name=builderPrefix('repo_setup'),
                slaves=branchConfig['platforms']['linux']['slaves'],
                category='release',
                ))

    if not releaseConfig.get('skip_tag'):
        tag_factory = ReleaseTaggingFactory(
            hgHost=branchConfig['hghost'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            repositories=tag_repositories,
            productName=releaseConfig['productName'],
            appName=releaseConfig['appName'],
            version=releaseConfig['version'],
            appVersion=releaseConfig['appVersion'],
            milestone=releaseConfig['milestone'],
            baseTag=releaseConfig['baseTag'],
            buildNumber=releaseConfig['buildNumber'],
            hgUsername=releaseConfig['hgUsername'],
            hgSshKey=releaseConfig['hgSshKey'],
            clobberURL=branchConfig['base_clobber_url'],
        )

        builders.append({
            'name': builderPrefix('tag'),
            'slavenames': branchConfig['platforms']['linux']['slaves'],
            'category': 'release',
            'builddir': builderPrefix('tag'),
            'factory': tag_factory,
            'nextSlave': _nextFastSlave,
        })
    else:
        builders.append(makeDummyBuilder(
            name=builderPrefix('tag'),
            slaves=branchConfig['platforms']['linux']['slaves'],
            category='release',
            ))

    if not releaseConfig.get('skip_source'):
        source_factory = SingleSourceFactory(
            hgHost=branchConfig['hghost'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            repoPath=releaseConfig['sourceRepoPath'],
            productName=releaseConfig['productName'],
            version=releaseConfig['version'],
            baseTag=releaseConfig['baseTag'],
            stagingServer=branchConfig['stage_server'],
            stageUsername=branchConfig['stage_username'],
            stageSshKey=branchConfig['stage_ssh_key'],
            buildNumber=releaseConfig['buildNumber'],
            autoconfDirs=['.', 'js/src'],
            clobberURL=branchConfig['base_clobber_url'],
        )

        builders.append({
           'name': builderPrefix('source'),
            'slavenames': branchConfig['platforms']['linux']['slaves'],
           'category': 'release',
           'builddir': builderPrefix('source'),
           'factory': source_factory,
           'nextSlave': _nextFastSlave,
        })

        if releaseConfig['xulrunnerPlatforms']:
            xulrunner_source_factory = SingleSourceFactory(
                hgHost=branchConfig['hghost'],
                buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                repoPath=releaseConfig['sourceRepoPath'],
                productName='xulrunner',
                version=releaseConfig['milestone'],
                baseTag=releaseConfig['baseTag'],
                stagingServer=branchConfig['stage_server'],
                stageUsername=branchConfig['stage_username_xulrunner'],
                stageSshKey=branchConfig['stage_ssh_xulrunner_key'],
                buildNumber=releaseConfig['buildNumber'],
                autoconfDirs=['.', 'js/src'],
                clobberURL=branchConfig['base_clobber_url'],
            )

            builders.append({
               'name': builderPrefix('xulrunner_source'),
               'slavenames': branchConfig['platforms']['linux']['slaves'],
               'category': 'release',
               'builddir': builderPrefix('xulrunner_source'),
               'factory': xulrunner_source_factory
            })
    else:
        builders.append(makeDummyBuilder(
            name=builderPrefix('source'),
            slaves=branchConfig['platforms']['linux']['slaves'],
            category='release',
            ))
        if releaseConfig['xulrunnerPlatforms']:
            builders.append(makeDummyBuilder(
                name=builderPrefix('xulrunner_source'),
                slaves=branchConfig['platforms']['linux']['slaves'],
                category='release',
                ))

    for platform in releaseConfig['enUSPlatforms']:
        # shorthand
        pf = branchConfig['platforms'][platform]
        mozconfig = '%s/%s/release' % (platform, releaseConfig['sourceRepoName'])
        if platform in releaseConfig['talosTestPlatforms']:
            talosMasters = branchConfig['talos_masters']
        else:
            talosMasters = None

        if platform in releaseConfig['unittestPlatforms']:
            packageTests = True
            unittestMasters = branchConfig['unittest_masters']
            unittestBranch = builderPrefix('%s-opt-unittest' % platform)
        else:
            packageTests = False
            unittestMasters = None
            unittestBranch = None

        if not releaseConfig.get('skip_build'):
            build_factory = ReleaseBuildFactory(
                env=pf['env'],
                objdir=pf['platform_objdir'],
                platform=platform,
                hgHost=branchConfig['hghost'],
                repoPath=releaseConfig['sourceRepoPath'],
                buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                configRepoPath=branchConfig['config_repo_path'],
                configSubDir=branchConfig['config_subdir'],
                profiledBuild=pf['profiled_build'],
                mozconfig=mozconfig,
                buildRevision='%s_RELEASE' % releaseConfig['baseTag'],
                stageServer=branchConfig['stage_server'],
                stageUsername=branchConfig['stage_username'],
                stageGroup=branchConfig['stage_group'],
                stageSshKey=branchConfig['stage_ssh_key'],
                stageBasePath=branchConfig['stage_base_path'],
                codesighs=False,
                uploadPackages=True,
                uploadSymbols=True,
                createSnippet=False,
                doCleanup=True, # this will clean-up the mac build dirs, but not delete
                                # the entire thing
                buildSpace=10,
                productName=releaseConfig['productName'],
                version=releaseConfig['version'],
                buildNumber=releaseConfig['buildNumber'],
                talosMasters=talosMasters,
                packageTests=packageTests,
                unittestMasters=unittestMasters,
                unittestBranch=unittestBranch,
                clobberURL=branchConfig['base_clobber_url'],
            )

            builders.append({
                'name': builderPrefix('%s_build' % platform),
                'slavenames': pf['slaves'],
                'category': 'release',
                'builddir': builderPrefix('%s_build' % platform),
                'factory': build_factory,
                'nextSlave': _nextFastSlave,
            })
        else:
            builders.append(makeDummyBuilder(
                name=builderPrefix('%s_build' % platform),
                slaves=branchConfig['platforms']['linux']['slaves'],
                category='release',
                ))

        if platform in releaseConfig['l10nPlatforms']:
            repack_factory = ReleaseRepackFactory(
                hgHost=branchConfig['hghost'],
                project=releaseConfig['productName'],
                appName=releaseConfig['appName'],
                repoPath=releaseConfig['sourceRepoPath'],
                l10nRepoPath=releaseConfig['l10nRepoPath'],
                stageServer=branchConfig['stage_server'],
                stageUsername=branchConfig['stage_username'],
                stageSshKey=branchConfig['stage_ssh_key'],
                buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                compareLocalesRepoPath=branchConfig['compare_locales_repo_path'],
                compareLocalesTag=branchConfig['compare_locales_tag'],
                buildSpace=2,
                configRepoPath=branchConfig['config_repo_path'],
                configSubDir=branchConfig['config_subdir'],
                mozconfig=mozconfig,
                platform=platform + '-release',
                buildRevision='%s_RELEASE' % releaseConfig['baseTag'],
                version=releaseConfig['version'],
                buildNumber=releaseConfig['buildNumber'],
                tree='release',
                clobberURL=branchConfig['base_clobber_url'],
            )

            builders.append({
                'name': builderPrefix('%s_repack' % platform),
                'slavenames': branchConfig['l10n_slaves'][platform],
                'category': 'release',
                'builddir': builderPrefix('%s_repack' % platform),
                'factory': repack_factory,
                'nextSlave': _nextFastSlave,
            })

        if platform in releaseConfig['unittestPlatforms']:
            mochitestLeakThreshold = pf.get('mochitest_leak_threshold', None)
            crashtestLeakThreshold = pf.get('crashtest_leak_threshold', None)
            for suites_name, suites in branchConfig['unittest_suites']:
                # Release builds on mac don't have a11y enabled, do disable the mochitest-a11y test
                if platform.startswith('macosx') and 'mochitest-a11y' in suites:
                    suites = suites[:]
                    suites.remove('mochitest-a11y')

                test_builders.extend(generateTestBuilder(
                    branchConfig, 'release', platform, builderPrefix("%s_test" % platform),
                    builderPrefix("%s-opt-unittest" % platform),
                    suites_name, suites, mochitestLeakThreshold,
                    crashtestLeakThreshold))

    for platform in releaseConfig['xulrunnerPlatforms']:
        pf = branchConfig['platforms'][platform]
        xr_env = pf['env'].copy()
        xr_env['SYMBOL_SERVER_USER'] = branchConfig['stage_username_xulrunner']
        xr_env['SYMBOL_SERVER_PATH'] = branchConfig['symbol_server_xulrunner_path']
        xr_env['SYMBOL_SERVER_SSH_KEY'] = \
            xr_env['SYMBOL_SERVER_SSH_KEY'].replace(branchConfig['stage_ssh_key'],
                                                    branchConfig['stage_ssh_xulrunner_key'])
        if not releaseConfig.get('skip_build'):
            xulrunner_build_factory = XulrunnerReleaseBuildFactory(
                env=xr_env,
                objdir=pf['platform_objdir'],
                platform=platform,
                hgHost=branchConfig['hghost'],
                repoPath=releaseConfig['sourceRepoPath'],
                buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                configRepoPath=branchConfig['config_repo_path'],
                configSubDir=branchConfig['config_subdir'],
                profiledBuild=None,
                mozconfig = '%s/%s/xulrunner' % (platform, releaseConfig['sourceRepoName']),
                buildRevision='%s_RELEASE' % releaseConfig['baseTag'],
                stageServer=branchConfig['stage_server'],
                stageUsername=branchConfig['stage_username_xulrunner'],
                stageGroup=branchConfig['stage_group'],
                stageSshKey=branchConfig['stage_ssh_xulrunner_key'],
                stageBasePath=branchConfig['stage_base_path_xulrunner'],
                codesighs=False,
                uploadPackages=True,
                uploadSymbols=True,
                createSnippet=False,
                doCleanup=True, # this will clean-up the mac build dirs, but not delete
                                # the entire thing
                buildSpace=pf.get('build_space', branchConfig['default_build_space']),
                productName='xulrunner',
                version=releaseConfig['milestone'],
                buildNumber=releaseConfig['buildNumber'],
                clobberURL=branchConfig['base_clobber_url'],
                packageSDK=True,
            )
            builders.append({
                'name': builderPrefix('xulrunner_%s_build' % platform),
                'slavenames': pf['slaves'],
                'category': 'release',
                'builddir': builderPrefix('xulrunner_%s_build' % platform),
                'factory': xulrunner_build_factory
            })
        else:
            builders.append(makeDummyBuilder(
                name=builderPrefix('xulrunner_%s_build' % platform),
                slaves=branchConfig['platforms']['linux']['slaves'],
                category='release',
                ))

    if releaseConfig['doPartnerRepacks']:
        partner_repack_factory = PartnerRepackFactory(
            hgHost=branchConfig['hghost'],
            repoPath=releaseConfig['sourceRepoPath'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            productName=releaseConfig['productName'],
            version=releaseConfig['version'],
            buildNumber=releaseConfig['buildNumber'],
            partnersRepoPath=releaseConfig['partnersRepoPath'],
            stagingServer=releaseConfig['stagingServer'],
            stageUsername=branchConfig['stage_username'],
            stageSshKey=branchConfig['stage_ssh_key'],
        )

        if 'macosx64' in branchConfig['platforms']:
            slaves = branchConfig['platforms']['macosx64']['slaves']
        else:
            slaves = branchConfig['platforms']['macosx']['slaves']
        builders.append({
            'name': builderPrefix('partner_repack'),
            'slavenames': slaves,
            'category': 'release',
            'builddir': builderPrefix('partner_repack'),
            'factory': partner_repack_factory,
            'nextSlave': _nextFastSlave,
        })

    for platform in releaseConfig['l10nPlatforms']:
        l10n_verification_factory = L10nVerifyFactory(
            hgHost=branchConfig['hghost'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            cvsroot=releaseConfig['cvsroot'],
            stagingServer=releaseConfig['stagingServer'],
            productName=releaseConfig['productName'],
            version=releaseConfig['version'],
            buildNumber=releaseConfig['buildNumber'],
            oldVersion=releaseConfig['oldVersion'],
            oldBuildNumber=releaseConfig['oldBuildNumber'],
            clobberURL=branchConfig['base_clobber_url'],
            platform=platform,
        )

        if 'macosx64' in branchConfig['platforms']:
            slaves = branchConfig['platforms']['macosx64']['slaves']
        else:
            slaves = branchConfig['platforms']['macosx']['slaves']
        builders.append({
            'name': builderPrefix('l10n_verification', platform),
            'slavenames': slaves,
            'category': 'release',
            'builddir': builderPrefix('l10n_verification', platform),
            'factory': l10n_verification_factory,
            'nextSlave': _nextFastSlave,
        })


    updates_factory = ReleaseUpdatesFactory(
        hgHost=branchConfig['hghost'],
        repoPath=releaseConfig['sourceRepoPath'],
        buildToolsRepoPath=branchConfig['build_tools_repo_path'],
        cvsroot=releaseConfig['cvsroot'],
        patcherToolsTag=releaseConfig['patcherToolsTag'],
        patcherConfig=releaseConfig['patcherConfig'],
        verifyConfigs=releaseConfig['verifyConfigs'],
        appName=releaseConfig['appName'],
        productName=releaseConfig['productName'],
        version=releaseConfig['version'],
        appVersion=releaseConfig['appVersion'],
        baseTag=releaseConfig['baseTag'],
        buildNumber=releaseConfig['buildNumber'],
        oldVersion=releaseConfig['oldVersion'],
        oldAppVersion=releaseConfig['oldAppVersion'],
        oldBaseTag=releaseConfig['oldBaseTag'],
        oldBuildNumber=releaseConfig['oldBuildNumber'],
        ftpServer=releaseConfig['ftpServer'],
        bouncerServer=releaseConfig['bouncerServer'],
        stagingServer=releaseConfig['stagingServer'],
        useBetaChannel=releaseConfig['useBetaChannel'],
        stageUsername=branchConfig['stage_username'],
        stageSshKey=branchConfig['stage_ssh_key'],
        ausUser=releaseConfig['ausUser'],
        ausSshKey=releaseConfig['ausSshKey'],
        ausHost=branchConfig['aus2_host'],
        ausServerUrl=releaseConfig['ausServerUrl'],
        hgSshKey=releaseConfig['hgSshKey'],
        hgUsername=releaseConfig['hgUsername'],
        # We disable this on staging, because we don't have a CVS mirror to
        # commit to
        commitPatcherConfig=(not staging),
        clobberURL=branchConfig['base_clobber_url'],
        oldRepoPath=releaseConfig['sourceRepoPath'],
        releaseNotesUrl=releaseConfig['releaseNotesUrl'],
        binaryName=releaseConfig['binaryName'],
        oldBinaryName=releaseConfig['oldBinaryName'],
        testOlderPartials=releaseConfig['testOlderPartials'],
    )

    builders.append({
        'name': builderPrefix('updates'),
        'slavenames': branchConfig['platforms']['linux']['slaves'],
        'category': 'release',
        'builddir': builderPrefix('updates'),
        'factory': updates_factory,
        'nextSlave': _nextFastSlave,
    })


    for platform in sorted(releaseConfig['verifyConfigs'].keys()):
        update_verify_factory = UpdateVerifyFactory(
            hgHost=branchConfig['hghost'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            verifyConfig=releaseConfig['verifyConfigs'][platform],
            clobberURL=branchConfig['base_clobber_url'],
        )

        builders.append({
            'name': builderPrefix('%s_update_verify' % platform),
            'slavenames': branchConfig['platforms'][platform]['slaves'],
            'category': 'release',
            'builddir': builderPrefix('%s_update_verify' % platform),
            'factory': update_verify_factory,
            'nextSlave': _nextFastSlave,
        })


    final_verification_factory = ReleaseFinalVerification(
        hgHost=branchConfig['hghost'],
        buildToolsRepoPath=branchConfig['build_tools_repo_path'],
        verifyConfigs=releaseConfig['verifyConfigs'],
        clobberURL=branchConfig['base_clobber_url'],
    )

    builders.append({
        'name': builderPrefix('final_verification'),
        'slavenames': branchConfig['platforms']['linux']['slaves'],
        'category': 'release',
        'builddir': builderPrefix('final_verification'),
        'factory': final_verification_factory,
        'nextSlave': _nextFastSlave,
    })

    if releaseConfig['majorUpdateRepoPath']:
        # Not attached to any Scheduler
        major_update_factory = MajorUpdateFactory(
            hgHost=branchConfig['hghost'],
            repoPath=releaseConfig['majorUpdateRepoPath'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            cvsroot=releaseConfig['cvsroot'],
            patcherToolsTag=releaseConfig['patcherToolsTag'],
            patcherConfig=releaseConfig['majorUpdatePatcherConfig'],
            verifyConfigs=releaseConfig['majorUpdateVerifyConfigs'],
            appName=releaseConfig['appName'],
            productName=releaseConfig['productName'],
            version=releaseConfig['majorUpdateToVersion'],
            appVersion=releaseConfig['majorUpdateAppVersion'],
            baseTag=releaseConfig['majorUpdateBaseTag'],
            buildNumber=releaseConfig['majorUpdateBuildNumber'],
            oldVersion=releaseConfig['version'],
            oldAppVersion=releaseConfig['appVersion'],
            oldBaseTag=releaseConfig['baseTag'],
            oldBuildNumber=releaseConfig['buildNumber'],
            ftpServer=releaseConfig['ftpServer'],
            bouncerServer=releaseConfig['bouncerServer'],
            stagingServer=releaseConfig['stagingServer'],
            useBetaChannel=releaseConfig['useBetaChannel'],
            stageUsername=branchConfig['stage_username'],
            stageSshKey=branchConfig['stage_ssh_key'],
            ausUser=releaseConfig['ausUser'],
            ausSshKey=releaseConfig['ausSshKey'],
            ausHost=branchConfig['aus2_host'],
            ausServerUrl=releaseConfig['ausServerUrl'],
            hgSshKey=releaseConfig['hgSshKey'],
            hgUsername=releaseConfig['hgUsername'],
            # We disable this on staging, because we don't have a CVS mirror to
            # commit to
            commitPatcherConfig=(not staging),
            clobberURL=branchConfig['base_clobber_url'],
            oldRepoPath=releaseConfig['sourceRepoPath'],
            triggerSchedulers=['major_update_verify'],
            releaseNotesUrl=releaseConfig['majorUpdateReleaseNotesUrl'],
        )

        builders.append({
            'name': builderPrefix('major_update'),
            'slavenames': branchConfig['platforms']['linux']['slaves'],
            'category': 'release',
            'builddir': builderPrefix('major_update'),
            'factory': major_update_factory,
            'nextSlave': _nextFastSlave,
        })

        for platform in sorted(releaseConfig['majorUpdateVerifyConfigs'].keys()):
            major_update_verify_factory = UpdateVerifyFactory(
                hgHost=branchConfig['hghost'],
                buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                verifyConfig=releaseConfig['majorUpdateVerifyConfigs'][platform],
                clobberURL=branchConfig['base_clobber_url'],
            )

            builders.append({
                'name': builderPrefix('%s_major_update_verify' % platform),
                'slavenames': branchConfig['platforms'][platform]['slaves'],
                'category': 'release',
                'builddir': builderPrefix('%s_major_update_verify' % platform),
                'factory': major_update_verify_factory,
                'nextSlave': _nextFastSlave,
            })

    bouncer_submitter_factory = TuxedoEntrySubmitterFactory(
        baseTag=releaseConfig['baseTag'],
        appName=releaseConfig['appName'],
        config=releaseConfig['tuxedoConfig'],
        productName=releaseConfig['productName'],
        version=releaseConfig['version'],
        milestone=releaseConfig['milestone'],
        tuxedoServerUrl=releaseConfig['tuxedoServerUrl'],
        enUSPlatforms=releaseConfig['enUSPlatforms'],
        l10nPlatforms=releaseConfig['l10nPlatforms'],
        oldVersion=releaseConfig['oldVersion'],
        hgHost=branchConfig['hghost'],
        repoPath=releaseConfig['sourceRepoPath'],
        buildToolsRepoPath=branchConfig['build_tools_repo_path'],
        credentialsFile=os.path.join(os.getcwd(), "BuildSlaves.py")
    )

    builders.append({
        'name': builderPrefix('bouncer_submitter'),
        'slavenames': branchConfig['platforms']['linux']['slaves'],
        'category': 'release',
        'builddir': builderPrefix('bouncer_submitter'),
        'factory': bouncer_submitter_factory
    })


    status.append(TinderboxMailNotifier(
        fromaddr="mozilla2.buildbot@build.mozilla.org",
        tree=branchConfig["tinderbox_tree"] + "-Release",
        extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org",],
        relayhost="mail.build.mozilla.org",
        builders=[b['name'] for b in builders],
        logCompression="bzip2")
    )

    status.append(TinderboxMailNotifier(
        fromaddr="mozilla2.buildbot@build.mozilla.org",
        tree=branchConfig["tinderbox_tree"] + "-Release",
        extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org",],
        relayhost="mail.build.mozilla.org",
        builders=[b['name'] for b in test_builders],
        logCompression="bzip2",
        errorparser="unittest")
    )

    builders.extend(test_builders)

    return {
            "builders": builders,
            "status": status,
            "change_source": change_source,
            "schedulers": schedulers,
            }