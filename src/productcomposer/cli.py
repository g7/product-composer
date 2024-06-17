""" Implementation of the command line interface.

"""

import os
import re
import shutil
import subprocess
import gettext
from datetime import datetime
from argparse import ArgumentParser
from xml.etree import ElementTree as ET

import yaml

from .core.logger import logger
from .core.PkgSet import PkgSet
from .core.Package import Package
from .core.Pool import Pool
from .wrappers import CreaterepoWrapper
from .wrappers import ModifyrepoWrapper


__all__ = "main",


ET_ENCODING = "unicode"


tree_report = {}        # hashed via file name

# hardcoded defaults for now
chksums_tool = 'sha512sum'

# global db for supportstatus
supportstatus = {}
# per package override via supportstatus.txt file
supportstatus_override = {}


def main(argv=None) -> int:
    """ Execute the application CLI.

    :param argv: argument list to parse (sys.argv by default)
    :return: exit status
    """
    #
    # Setup CLI parser
    #
    parser = ArgumentParser('productcomposer', description='An example sub-command implementation')
    subparsers = parser.add_subparsers(required=True, help='sub-command help')

    # One sub parser for each command
    verify_parser = subparsers.add_parser('verify', help='The first sub-command')
    build_parser = subparsers.add_parser('build', help='The second sub-command')

    verify_parser.set_defaults(func=verify)
    build_parser.set_defaults(func=build)

    # Generic options
    for cmd_parser in [verify_parser, build_parser]:
        cmd_parser.add_argument('-f', '--flavor', help='Build a given flavor')
        cmd_parser.add_argument('-v', '--verbose', action='store_true',  help='Enable verbose output')
        cmd_parser.add_argument('--reposdir', action='store',  help='Take packages from this directory')
        cmd_parser.add_argument('filename', default='default.productcompose',  help='Filename of product YAML spec')

    # build command options
    build_parser.add_argument('-r', '--release', default=None,  help='Define a build release counter')
    build_parser.add_argument('--disturl', default=None,  help='Define a disturl')
    build_parser.add_argument('--vcs', default=None,  help='Define a source repository identifier')
    build_parser.add_argument('--clean', action='store_true',  help='Remove existing output directory first')
    build_parser.add_argument('out',  help='Directory to write the result')

    # parse and check
    args = parser.parse_args(argv)
    filename = args.filename
    if not filename:
        # No subcommand was specified.
        print("No filename")
        parser.print_help()
        die(None)

    #
    # Invoke the function
    #
    args.func(args)
    return 0


def die(msg, details=None):
    if msg:
        print("ERROR: " + msg)
    if details:
        print(details)
    raise SystemExit(1)


def warn(msg, details=None):
    print("WARNING: " + msg)
    if details:
        print(details)


def note(msg):
    print(msg)


def build(args):
    flavor = None
    if args.flavor:
        f = args.flavor.split('.')
        if f[0] != '':
            flavor = f[0]

    if not args.out:
        # No subcommand was specified.
        print("No output directory given")
        parser.print_help()
        die(None)

    yml = parse_yaml(args.filename, flavor)

    directory = os.getcwd()
    if args.filename.startswith('/'):
        directory = os.path.dirname(args.filename)
    reposdir = args.reposdir if args.reposdir else directory + "/repos"

    supportstatus_fn = os.path.join(directory, 'supportstatus.txt')
    if os.path.isfile(supportstatus_fn):
        parse_supportstatus(supportstatus_fn)

    pool = Pool()
    note(f"scanning: {reposdir}")
    pool.scan(reposdir)

    if args.clean and os.path.exists(args.out):
        shutil.rmtree(args.out)

    product_base_dir = get_product_dir(yml, flavor, args.release)

    create_tree(args.out, product_base_dir, yml, pool, flavor, args.vcs, args.disturl)


def verify(args):
    parse_yaml(args.filename, args.flavor)


def parse_yaml(filename, flavor):

    with open(filename, 'r') as file:
        yml = yaml.safe_load(file)

    if 'product_compose_schema' not in yml:
        die('missing product composer schema')
    if yml['product_compose_schema'] != 0 and yml['product_compose_schema'] != 0.1 and yml['product_compose_schema'] != 0.2:
        die(f"Unsupported product composer schema: {yml['product_compose_schema']}")

    if 'flavors' not in yml:
        yml['flavors'] = []

    if flavor:
        if flavor not in yml['flavors']:
            die("Flavor not found: " + flavor)
        f = yml['flavors'][flavor]
        # overwrite global values from flavor overwrites
        if 'architectures' in f:
            yml['architectures'] = f['architectures']
        if 'name' in f:
            yml['name'] = f['name']
        if 'summary' in f:
            yml['summary'] = f['summary']
        if 'version' in f:
            yml['version'] = f['version']
        if 'product-type' in f:
            yml['product-type'] = f['product-type']
        if 'product_directory_name' in f:
            yml['product_directory_name'] = f['product_directory_name']

    if 'architectures' not in yml or not yml['architectures']:
        die("No architecture defined. Maybe wrong flavor?")

    if 'build_options' not in yml:
        yml['build_options'] = []

    return yml


def parse_supportstatus(filename):
    with open(filename, 'r') as file:
        for line in file.readlines():
            a = line.strip().split(' ')
            supportstatus_override[a[0]] = a[1]


def get_product_dir(yml, flavor, release):
    name = yml['name'] + "-" + str(yml['version'])
    if 'product_directory_name' in yml:
        # manual override
        name = yml['product_directory_name']
    if flavor and not 'hide_flavor_in_product_directory_name' in yml['build_options']:
        name += "-" + flavor
    if yml['architectures']:
        visible_archs = yml['architectures']
        if 'local' in visible_archs:
            visible_archs.remove('local')
        name += "-" + "-".join(visible_archs)
    if release:
        name += "-Build" + str(release)
    if '/' in name:
        die("Illegal product name")
    return name


def run_helper(args, cwd=None, stdout=None, stdin=None, failmsg=None):
    if stdout is None:
        stdout = subprocess.PIPE
    if stdin is None:
        stdin = subprocess.PIPE
    popen = subprocess.Popen(args, stdout=stdout, stdin=stdin, cwd=cwd)
    if popen.wait():
        output = popen.stdout.read()
        if failmsg:
            die("Failed to " + failmsg, details=output)
        else:
            die("Failed to run" + args[0], details=output)
    return popen.stdout.read() if stdout == subprocess.PIPE else ''


def create_tree(outdir, product_base_dir, yml, pool, flavor, vcs=None, disturl=None):
    if not os.path.exists(outdir):
        os.mkdir(outdir)

    maindir = outdir + '/' + product_base_dir
    if not os.path.exists(maindir):
        os.mkdir(maindir)

    workdirectories = [ maindir ]
    debugdir = sourcedir = None
    if "source" in yml:
        if yml['source'] == 'split':
            sourcedir = outdir + '/' + product_base_dir + '-Source'
            os.mkdir(sourcedir)
            workdirectories.append(sourcedir)
        elif yml['source'] == 'include':
            sourcedir = maindir
        elif yml['source'] != 'drop':
            die("Bad source option, must be either 'include', 'split' or 'drop'")
    if "debug" in yml:
        if yml['debug'] == 'split':
            debugdir = outdir + '/' + product_base_dir + '-Debug'
            os.mkdir(debugdir)
            workdirectories.append(debugdir)
        elif yml['debug'] == 'include':
            debugdir = maindir
        elif yml['debug'] != 'drop':
            die("Bad debug option, must be either 'include', 'split' or 'drop'")

    for arch in yml['architectures']:
        link_rpms_to_tree(maindir, yml, pool, arch, flavor, debugdir, sourcedir)

    for arch in yml['architectures']:
        unpack_meta_rpms(maindir, yml, pool, arch, flavor, medium=1)  # only for first medium am

    repos = []
    if disturl:
        match = re.match("^obs://([^/]*)/([^/]*)/.*", disturl)
        if match:
            obsname = match.group(1)
            project = match.group(2)
            repo = f"obsproduct://{obsname}/{project}/{yml['name']}/{yml['version']}"
            repos = [repo]
    if vcs:
        repos.append(vcs)

    default_content = ["pool"]
    for file in os.listdir(maindir):
        if not file.startswith('gpg-pubkey-'):
            continue

        args = ['gpg', '--no-keyring', '--no-default-keyring', '--with-colons',
              '--import-options', 'show-only', '--import', '--fingerprint']
        out = run_helper(args, stdin=open(f'{maindir}/{file}', 'rb'),
                         failmsg="Finger printing of gpg file")
        for line in out.splitlines():
            if not str(line).startswith("b'fpr:"):
                continue

            default_content.append(str(line).split(':')[9])

    run_createrepo(maindir, yml, content=default_content, repos=repos)
    if debugdir:
        run_createrepo(debugdir, yml, content=["debug"], repos=repos)
    if sourcedir:
        run_createrepo(sourcedir, yml, content=["source"], repos=repos)

    if not os.path.exists(maindir + '/repodata'):
        die("run_createrepo did not create a repodata directory");

    write_report_file(maindir, maindir + '.report')
    if sourcedir and maindir != sourcedir:
        write_report_file(sourcedir, sourcedir + '.report')
    if debugdir and maindir != debugdir:
        write_report_file(debugdir, debugdir + '.report')

    # CHANGELOG file
    # the tools read the subdirectory of the maindir from environment variable
    os.environ['ROOT_ON_CD'] = '.'
    if os.path.exists("/usr/bin/mk_changelog"):
        args = ["/usr/bin/mk_changelog", maindir]
        run_helper(args)

    # ARCHIVES.gz
    if os.path.exists("/usr/bin/mk_listings"):
        args = ["/usr/bin/mk_listings", maindir]
        run_helper(args)

    # media.X structures FIXME
    mediavendor = yml['vendor'] + ' - ' + product_base_dir
    mediaident = product_base_dir
    # FIXME: calculate from product provides
    mediaproducts = [yml['vendor'] + '-' + yml['name'] + ' ' + str(yml['version']) + '-1']
    create_media_dir(maindir, mediavendor, mediaident, mediaproducts)

    create_checksums_file(maindir)

    create_susedata_xml(maindir, yml)
    if debugdir:
        create_susedata_xml(debugdir, yml)
    if sourcedir:
        create_susedata_xml(sourcedir, yml)

    create_updateinfo_xml(maindir, yml, pool, flavor, debugdir, sourcedir)

    # Add License File and create extra .license directory
    licensefilename = '/license.tar'
    if os.path.exists(maindir + '/license-' + yml['name'] + '.tar') or os.path.exists(maindir + '/license-' + yml['name'] + '.tar.gz'):
        licensefilename = '/license-' + yml['name'] + '.tar'
    if os.path.exists(maindir + licensefilename + '.gz'):
        run_helper(['gzip', '-d', maindir + licensefilename + '.gz'],
                   failmsg="Uncompress of license.tar.gz failed")
    if os.path.exists(maindir + licensefilename):
        licensedir = maindir + ".license"
        if not os.path.exists(licensedir):
            os.mkdir(licensedir)
        args = ['tar', 'xf', maindir + licensefilename, '-C', licensedir]
        output = run_helper(args, failmsg="extract license tar ball")
        if not os.path.exists(licensedir + "/license.txt"):
            die("No license.txt extracted", details=output)

        mr = ModifyrepoWrapper(
            file=maindir + licensefilename,
            directory=os.path.join(maindir, "repodata"),
        )
        mr.run_cmd()
        os.unlink(maindir + licensefilename)
        # meta package may bring a second file or expanded symlink, so we need clean up
        if os.path.exists(maindir + '/license.tar'):
            os.unlink(maindir + '/license.tar')
        if os.path.exists(maindir + '/license.tar.gz'):
            os.unlink(maindir + '/license.tar.gz')

    for workdir in workdirectories:
        # detached signature
        args = ['/usr/lib/build/signdummy', '-d', workdir + "/repodata/repomd.xml"]
        run_helper(args, failmsg="create detached signature")
        if os.path.exists(workdir + '/CHECKSUMS'):
            args = ['/usr/lib/build/signdummy', '-d', workdir + '/CHECKSUMS']
            run_helper(args, failmsg="create detached signature for CHECKSUMS")

        # pubkey
        with open(workdir + "/repodata/repomd.xml.key", 'w') as pubkey_file:
            args = ['/usr/lib/build/signdummy', '-p']
            run_helper(args, stdout=pubkey_file, failmsg="write signature public key")

        # do we need an ISO file?
        if 'iso' in yml:
            application_id = re.sub(r'^.*/', '', maindir)
            args = ['/usr/bin/mkisofs', '-quiet', '-p', 'Product Composer - http://www.github.com/openSUSE/product-composer']
            args += ['-r', '-pad', '-f', '-J', '-joliet-long']
            # FIXME: do proper multi arch handling
            isolinux = 'boot/' + yml['architectures'][0] + '/loader/isolinux.bin'
            if os.path.isfile(workdir + '/' + isolinux):
                args += ['-no-emul-boot', '-boot-load-size', '4', '-boot-info-table']
                args += ['-hide', 'glump', '-hide-joliet', 'glump']
                args += ['-eltorito-alt-boot', '-eltorito-platform', 'efi']
                args += ['-no-emul-boot']
                # args += [ '-sort', $sort_file ]
                # args += [ '-boot-load-size', block_size("boot/"+arch+"/loader") ]
                args += ['-b', isolinux]
            if 'publisher' in yml['iso']:
                args += ['-publisher', yml['iso']['publisher']]
            if 'volume_id' in yml['iso']:
                args += ['-V', yml['iso']['volume_id']]
            args += ['-A', application_id]
            args += ['-o', workdir + '.iso', workdir]
            run_helper(args, cwd=outdir, failmsg="create iso file")
            # simple tag media call ... we may add options for pading or triggering media check later
            args = [ 'tagmedia' , '--digest' , 'sha256', workdir + '.iso' ]
            run_helper(args, cwd=outdir, failmsg="tagmedia iso file")
            # creating .sha256 for iso file
            with open(workdir + ".iso.sha256", 'w') as sha_file:
                # argument must not have the path
                args = [ 'sha256sum', workdir.split('/')[-1] + '.iso' ]
                run_helper(args, cwd=outdir, stdout=sha_file, failmsg="create .iso.sha256 file")

    # create SBOM data
    if os.path.exists("/usr/lib/build/generate_sbom"):
        spdx_distro = "ALP"
        spdx_distro += "-" + str(yml['version'])
        # SPDX
        args = ["/usr/lib/build/generate_sbom",
                 "--format", 'spdx',
                 "--distro", spdx_distro,
                 "--product", maindir
               ]
        with open(maindir + ".spdx.json", 'w') as sbom_file:
            run_helper(args, stdout=sbom_file, failmsg="run generate_sbom for SPDX")

        # CycloneDX
        args = ["/usr/lib/build/generate_sbom",
                 "--format", 'cyclonedx',
                 "--distro", spdx_distro,
                 "--product", maindir
               ]
        with open(maindir + ".cdx.json", 'w') as sbom_file:
            run_helper(args, stdout=sbom_file, failmsg="run generate_sbom for CycloneDX")

# create media info files


def create_media_dir(maindir, vendorstr, identstr, products):
    media1dir = maindir + '/' + 'media.1'
    if not os.path.isdir(media1dir):
        os.mkdir(media1dir)  # we do only support seperate media atm
    with open(media1dir + '/media', 'w') as media_file:
        media_file.write(vendorstr + "\n")
        media_file.write(identstr + "\n")
        media_file.write("1\n")
    if products:
        with open(media1dir + '/products', 'w') as products_file:
            for productname in products:
                products_file.write('/ ' + productname + "\n")


def create_checksums_file(maindir):
    with open(maindir + '/CHECKSUMS', 'a') as chksums_file:
        for subdir in ('boot', 'EFI', 'docu', 'media.1'):
            if not os.path.exists(maindir + '/' + subdir):
                continue
            for root, dirnames, filenames in os.walk(maindir + '/' + subdir):
                for name in filenames:
                    relname = os.path.relpath(root + '/' + name, maindir)
                    run_helper([chksums_tool, relname], cwd=maindir, stdout=chksums_file)

# create a fake package entry from an updateinfo package spec


def create_updateinfo_package(pkgentry):
    entry = Package()
    for tag in 'name', 'epoch', 'version', 'release', 'arch':
        setattr(entry, tag, pkgentry.get(tag))
    return entry

def generate_du_data(pkg, maxdepth):
    dirs = pkg.get_directories()
    seen = set()
    dudata_size = {}
    dudata_count = {}
    for dir, filedatas in pkg.get_directories().items():
        size = 0
        count = 0
        for filedata in filedatas:
            (basename, filesize, cookie) = filedata
            if cookie:
                if cookie in seen:
                    next
                seen.add(cookie)
            size += filesize
            count += 1
        if dir == '':
            dir = '/usr/src/packages/'
        dir = '/' + dir.strip('/')
        subdir = ''
        depth = 0
        for comp in dir.split('/'):
            if comp == '' and subdir != '':
                next
            subdir += comp + '/'
            if subdir not in dudata_size:
                dudata_size[subdir] = 0
                dudata_count[subdir] = 0
            dudata_size[subdir] += size
            dudata_count[subdir] += count
            depth += 1
            if depth > maxdepth:
                break
    dudata = []
    for dir, size in sorted(dudata_size.items()):
        dudata.append((dir, size, dudata_count[dir]))
    return dudata

# Get supported translations based on installed packages
def get_package_translation_languages():
    i18ndir = '/usr/share/locale/en_US/LC_MESSAGES'
    p = re.compile('package-translations-(.+).mo')
    languages = set()
    for file in os.listdir(i18ndir):
        m = p.match(file)
        if m:
            languages.add(m.group(1))
    return sorted(list(languages))

# Create the main susedata.xml with translations, support, and disk usage information
def create_susedata_xml(rpmdir, yml):
    susedatas = {}
    susedatas_count = {}

    # find translation languages
    languages = get_package_translation_languages()

    # create gettext translator object
    i18ntrans = {}
    for lang in languages:
        i18ntrans[lang] = gettext.translation(f'package-translations-{lang}',
                                              languages=['en_US'])

    # read repomd.xml
    ns = '{http://linux.duke.edu/metadata/repo}'
    tree = ET.parse(rpmdir + '/repodata/repomd.xml')
    primary_fn = tree.find(f".//{ns}data[@type='primary']/{ns}location").get('href')

    # read compressed primary.xml
    openfunction = None
    if primary_fn.endswith('.gz'):
        import gzip
        openfunction = gzip.open
    elif primary_fn.endswith('.zst'):
        import zstandard
        openfunction = zstandard.open
    else:
        die(f"unsupported primary compression type ({primary_fm})")
    tree = ET.parse(openfunction(rpmdir + '/' + primary_fn, 'rb'))
    ns = '{http://linux.duke.edu/metadata/common}'

    # Create main susedata structure
    susedatas[''] = ET.Element('susedata')
    susedatas_count[''] = 0

    # go for every rpm file of the repo via the primary
    for pkg in tree.findall(f".//{ns}package[@type='rpm']"):
        name = pkg.find(f'{ns}name').text
        arch = pkg.find(f'{ns}arch').text
        pkgid = pkg.find(f'{ns}checksum').text
        version = pkg.find(f'{ns}version').attrib

        susedatas_count[''] += 1
        package = ET.SubElement(susedatas[''], 'package', {'name': name, 'arch': arch, 'pkgid': pkgid})
        ET.SubElement(package, 'version', version)

        # add supportstatus
        if name in supportstatus and supportstatus[name] is not None:
            ET.SubElement(package, 'keyword').text = f'support_{supportstatus[name]}'

        # add disk usage data
        location = pkg.find(f'{ns}location').get('href')
        if os.path.exists(rpmdir + '/' + location):
            p = Package()
            p.location = rpmdir + '/' + location
            dudata = generate_du_data(p, 3)
            if dudata:
                duelement = ET.SubElement(package, 'diskusage')
                dirselement = ET.SubElement(duelement, 'dirs')
                for duitem in dudata:
                    ET.SubElement(dirselement, 'dir', {'name': duitem[0], 'size': str(duitem[1]), 'count': str(duitem[2])})

        # get summary/description/category of the package
        summary = pkg.find(f'{ns}summary').text
        description = pkg.find(f'{ns}description').text
        category = pkg.find(".//{http://linux.duke.edu/metadata/rpm}entry[@name='pattern-category()']")
        category = Package._cpeid_hexdecode(category.get('ver')) if category else None

        # look for translations
        for lang in languages:
            isummary = i18ntrans[lang].gettext(summary)
            idescription = i18ntrans[lang].gettext(description)
            icategory = i18ntrans[lang].gettext(category) if category is not None else None
            if isummary == summary and idescription == description and icategory == category:
                continue
            if lang not in susedatas:
                susedatas[lang] = ET.Element('susedata')
                susedatas_count[lang] = 0
            susedatas_count[lang] += 1
            ipackage = ET.SubElement(susedatas[lang], 'package', {'name': name, 'arch': arch, 'pkgid': pkgid})
            ET.SubElement(ipackage, 'version', version)
            if isummary != summary:
                ET.SubElement(ipackage, 'summary', {'lang': lang}).text = isummary
            if idescription != description:
                ET.SubElement(ipackage, 'description', {'lang': lang}).text = idescription
            if icategory != category:
                ET.SubElement(ipackage, 'category', {'lang': lang}).text = icategory

    # write all susedata files
    for lang, susedata in sorted(susedatas.items()):
        susedata.set('xmlns', 'http://linux.duke.edu/metadata/susedata')
        susedata.set('packages', str(susedatas_count[lang]))
        ET.indent(susedata, space="    ", level=0)
        susedata_fn = rpmdir + (f'/susedata.{lang}.xml' if lang else '/susedata.xml')
        with open(susedata_fn, 'x') as sd_file:
            sd_file.write(ET.tostring(susedata, encoding=ET_ENCODING))
        mr = ModifyrepoWrapper(
            file=susedata_fn,
            directory=os.path.join(rpmdir, "repodata"),
        )
        mr.run_cmd()
        os.unlink(susedata_fn)


# Add updateinfo.xml to metadata
def create_updateinfo_xml(rpmdir, yml, pool, flavor, debugdir, sourcedir):
    if not pool.updateinfos:
        return

    missing_package = False

    # build the union of the package sets for all requested architectures
    main_pkgset = PkgSet('main')
    for arch in yml['architectures']:
        pkgset = main_pkgset.add(create_package_set(yml, arch, flavor, 'main'))
    main_pkgset_names = main_pkgset.names()

    uitemp = None

    for u in sorted(pool.lookup_all_updateinfos()):
        note("Add updateinfo " + u.location)
        for update in u.root.findall('update'):
            needed = False
            parent = update.findall('pkglist')[0].findall('collection')[0]

            # drop OBS internal patchinforef element
            for pr in update.findall('patchinforef'):
                update.remove(pr)

            if 'set_updateinfo_from' in yml:
                update.set('from', yml['set_updateinfo_from'])

            if 'set_updateinfo_id_prefix' in yml:
                id_node = update.find('id')
                # avoid double application of same prefix
                id_text = re.sub(r'^'+yml['set_updateinfo_id_prefix'], '', id_node.text)
                id_node.text = yml['set_updateinfo_id_prefix'] + id_text

            for pkgentry in parent.findall('package'):
                src = pkgentry.get('src')

                # check for embargo date
                embargo = pkgentry.find('embargo_date')
                if embargo is not None:
                    try:
                        embargo_time = datetime.strptime(embargo.text, '%Y-%m-%d %H:%M')
                    except ValueError:
                        embargo_time = datetime.strptime(embargo.text, '%Y-%m-%d')

                    if embargo_time > datetime.now():
                        print("WARNING: Update is still under embargo! ", update.find('id').text)
                        if 'block_updates_under_embargo' in yml['build_options']:
                            die("shutting down due to block_updates_under_embargo flag")

                # clean internal elements
                for internal_element in ['supportstatus', 'superseded_by', 'embargo_date']:
                    for e in pkgentry.findall(internal_element):
                        pkgentry.remove(e)

                # check if we have files for the entry
                if os.path.exists(rpmdir + '/' + src):
                    needed = True
                    continue
                if debugdir and os.path.exists(debugdir + '/' + src):
                    needed = True
                    continue
                if sourcedir and os.path.exists(sourcedir + '/' + src):
                    needed = True
                    continue
                name = pkgentry.get('name')
                pkgarch = pkgentry.get('arch')

                # do not insist on debuginfo or source packages
                if pkgarch == 'src' or pkgarch == 'nosrc':
                    parent.remove(pkgentry)
                    continue
                if name.endswith('-debuginfo') or name.endswith('-debugsource'):
                    parent.remove(pkgentry)
                    continue
                # ignore unwanted architectures
                if pkgarch != 'noarch' and pkgarch not in yml['architectures']:
                    parent.remove(pkgentry)
                    continue

                # check if we should have this package
                if name in main_pkgset_names:
                    updatepkg = create_updateinfo_package(pkgentry)
                    if main_pkgset.matchespkg(None, updatepkg):
                        warn(f"package {updatepkg} not found")
                        missing_package = True

                parent.remove(pkgentry)

            if not needed:
                continue

            if not uitemp:
                uitemp = open(rpmdir + '/updateinfo.xml', 'x')
                uitemp.write("<updates>\n  ")
            uitemp.write(ET.tostring(update, encoding=ET_ENCODING))

    if uitemp:
        uitemp.write("</updates>\n")
        uitemp.close()

        mr = ModifyrepoWrapper(
                file=os.path.join(rpmdir, "updateinfo.xml"),
                directory=os.path.join(rpmdir, "repodata"),
                )
        mr.run_cmd()

        os.unlink(rpmdir + '/updateinfo.xml')

    if missing_package and not 'ignore_missing_packages' in yml['build_options']:
        die('Abort due to missing packages')


def run_createrepo(rpmdir, yml, content=[], repos=[]):
    product_name = yml['name']
    product_summary = yml['summary'] or yml['name']
    product_summary += " " + str(yml['version'])

    product_type = '/o'
    if 'product-type' in yml:
      if yml['product-type'] == 'base':
        product_type = '/o'
      elif yml['product-type'] == 'module':
        product_type = '/a'
      else:
        die('Undefined product-type')
    cr = CreaterepoWrapper(directory=".")
    cr.distro = product_summary
    cr.cpeid = f"cpe:{product_type}:{yml['vendor']}:{yml['name']}:{yml['version']}"
    cr.repos = repos
# cr.split = True
    # cr.baseurl = "media://"
    cr.content = content
    cr.excludes = ["boot"]
    cr.run_cmd(cwd=rpmdir, stdout=subprocess.PIPE)


def unpack_one_meta_rpm(rpmdir, rpm, medium):
    tempdir = rpmdir + "/temp"
    os.mkdir(tempdir)
    run_helper(['unrpm', '-q', rpm.location], cwd=tempdir, failmsg=f"extract {rpm.location}")

    skel_dir = tempdir + "/usr/lib/skelcd/CD" + str(medium)
    if os.path.exists(skel_dir):
        shutil.copytree(skel_dir, rpmdir, dirs_exist_ok=True)
    shutil.rmtree(tempdir)


def unpack_meta_rpms(rpmdir, yml, pool, arch, flavor, medium):
    missing_package = False
    for unpack_pkgset_name in yml.get('unpack', []):
        unpack_pkgset = create_package_set(yml, arch, flavor, unpack_pkgset_name)
        for sel in unpack_pkgset:
            rpm = pool.lookup_rpm(arch, sel.name, sel.op, sel.epoch, sel.version, sel.release)
            if not rpm:
                warn(f"package {sel} not found")
                missing_package = True
                continue
            unpack_one_meta_rpm(rpmdir, rpm, medium)

    if missing_package and not 'ignore_missing_packages' in yml['build_options']:
        die('Abort due to missing packages')


def create_package_set_compat(yml, arch, flavor, setname):
    if setname == 'main':
        oldname = 'packages'
    elif setname == 'unpack':
        oldname = 'unpack_packages'
    else:
        return None
    if oldname not in yml:
        return PkgSet(setname) if setname == 'unpack' else None
    pkgset = PkgSet(setname)
    for entry in list(yml[oldname]):
        if type(entry) == dict:
            if 'flavors' in entry:
                if flavor is None or flavor not in entry['flavors']:
                    continue
            if 'architectures' in entry:
                if arch not in entry['architectures']:
                    continue
            pkgset.add_specs(entry['packages'])
        else:
            pkgset.add_specs([str(entry)])
    return pkgset


def create_package_set(yml, arch, flavor, setname):
    if 'packagesets' not in yml:
        pkgset = create_package_set_compat(yml, arch, flavor, setname)
        if pkgset is None:
            die(f'package set {setname} is not defined')
        return pkgset

    pkgsets = {}
    for entry in list(yml['packagesets']):
        name = entry['name'] if 'name' in entry else 'main'
        if name in pkgsets and pkgsets[name] is not None:
            die(f'package set {name} is already defined')
        pkgsets[name] = None
        if 'flavors' in entry:
            if flavor is None or flavor not in entry['flavors']:
                continue
        if 'architectures' in entry:
            if arch not in entry['architectures']:
                continue
        pkgset = PkgSet(name)
        pkgsets[name] = pkgset
        if 'supportstatus' in entry:
            pkgset.supportstatus = entry['supportstatus']
        if 'packages' in entry and entry['packages']:
            pkgset.add_specs(entry['packages'])
        for setop in 'add', 'sub', 'intersect':
            if setop not in entry:
                continue
            for oname in entry[setop]:
                if oname == name or oname not in pkgsets:
                    die(f'package set {oname} does not exist')
                if pkgsets[oname] is None:
                    pkgsets[oname] = PkgSet(oname)      # instantiate
                if setop == 'add':
                    pkgset.add(pkgsets[oname])
                elif setop == 'sub':
                    pkgset.sub(pkgsets[oname])
                elif setop == 'intersect':
                    pkgset.intersect(pkgsets[oname])
                else:
                    die(f"unsupported package set operation '{setop}'")

    if setname not in pkgsets:
        die(f'package set {setname} is not defined')
    if pkgsets[setname] is None:
        pkgsets[setname] = PkgSet(setname)      # instantiate
    return pkgsets[setname]


def link_rpms_to_tree(rpmdir, yml, pool, arch, flavor, debugdir=None, sourcedir=None):
    singlemode = True
    if 'take_all_available_versions' in yml['build_options']:
        singlemode = False
    add_slsa = False
    if 'add_slsa_provenance' in yml['build_options']:
        add_slsa = True

    main_pkgset = create_package_set(yml, arch, flavor, 'main')

    missing_package = None
    for sel in main_pkgset:
        if singlemode:
            rpm = pool.lookup_rpm(arch, sel.name, sel.op, sel.epoch, sel.version, sel.release)
            rpms = [rpm] if rpm else []
        else:
            rpms = pool.lookup_all_rpms(arch, sel.name, sel.op, sel.epoch, sel.version, sel.release)

        if not rpms:
            warn(f"package {sel} not found for {arch}")
            missing_package = True
            continue

        for rpm in rpms:
            link_entry_into_dir(rpm, rpmdir, add_slsa=add_slsa)
            if rpm.name in supportstatus_override:
                supportstatus[rpm.name] = supportstatus_override[rpm.name]
            else:
                supportstatus[rpm.name] = sel.supportstatus

            srcrpm = rpm.get_src_package()
            if not srcrpm:
                warn(f"package {rpm} does not have a source rpm")
                continue

            if sourcedir:
                # so we need to add also the src rpm
                srpm = pool.lookup_rpm(srcrpm.arch, srcrpm.name, '=', None, srcrpm.version, srcrpm.release)
                if srpm:
                    link_entry_into_dir(srpm, sourcedir, add_slsa=add_slsa)
                else:
                    details = f"         required by  {rpm}"
                    warn(f"source rpm package {srcrpm} not found", details=details)
                    missing_package = True

            if debugdir:
                drpm = pool.lookup_rpm(arch, srcrpm.name + "-debugsource", '=', None, srcrpm.version, srcrpm.release)
                if drpm:
                    link_entry_into_dir(drpm, debugdir, add_slsa=add_slsa)

                drpm = pool.lookup_rpm(arch, rpm.name + "-debuginfo", '=', rpm.epoch, rpm.version, rpm.release)
                if drpm:
                    link_entry_into_dir(drpm, debugdir, add_slsa=add_slsa)

    if missing_package and not 'ignore_missing_packages' in yml['build_options']:
        die('Abort due to missing packages')


def link_file_into_dir(filename, directory):
    if not os.path.exists(directory):
        os.mkdir(directory)
    outname = directory + '/' + os.path.basename(filename)
    if not os.path.exists(outname):
        if os.path.islink(filename):
            # osc creates a repos/ structure with symlinks to it's cache
            # but these would point outside of our media
            shutil.copyfile(filename, outname)
        else:
            os.link(filename, outname)


def link_entry_into_dir(entry, directory, add_slsa=False):
    outname = directory + '/' + entry.arch + '/' + os.path.basename(entry.location)
    if not os.path.exists(outname):
        link_file_into_dir(entry.location, directory + '/' + entry.arch)
        add_entry_to_report(entry, outname)
        if add_slsa:
            slsaname = entry.location.removesuffix('.rpm') + '.slsa_provenance.json'
            if os.path.exists(slsaname):
                link_file_into_dir(slsaname, directory + '/' + entry.arch)

def add_entry_to_report(entry, outname):
    # first one wins, see link_file_into_dir
    if outname not in tree_report:
        tree_report[outname] = entry


def write_report_file(directory, outfile):
    root = ET.Element('report')
    if not directory.endswith('/'):
        directory += '/'
    for fn, entry in sorted(tree_report.items()):
        if not fn.startswith(directory):
            continue
        binary = ET.SubElement(root, 'binary')
        binary.text = 'obs://' + entry.origin
        for tag in 'name', 'epoch', 'version', 'release', 'arch', 'buildtime', 'disturl', 'license':
            val = getattr(entry, tag, None)
            if val is None or val == '':
                continue
            if tag == 'epoch' and val == 0:
                continue
            if tag == 'arch':
                binary.set('binaryarch', str(val))
            else:
                binary.set(tag, str(val))
        if entry.name.endswith('-release'):
            cpeid = entry.product_cpeid
            if cpeid:
                binary.set('cpeid', cpeid)
    tree = ET.ElementTree(root)
    tree.write(outfile)


if __name__ == "__main__":
    try:
        status = main()
    except Exception as err:
        # Error handler of last resort.
        logger.error(repr(err))
        logger.critical("shutting down due to fatal error")
        raise  # print stack trace
    else:
        raise SystemExit(status)

# vim: sw=4 et
