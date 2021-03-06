"""
Module with helper functions

Some functions are derived from dac2bids.py from Daniel Gomez 29.08.2016
https://github.com/dangom/dac2bids/blob/master/dac2bids.py

@author: Marcel Zwiers
"""

import copy
import inspect
import ast
import re
import logging
import coloredlogs
import subprocess
import nibabel
import tempfile
import tarfile
import zipfile
import fnmatch
import pydicom
from pydicom.fileset import FileSet
from distutils.dir_util import copy_tree
from typing import Union, List, Tuple
from pathlib import Path
from importlib import util
try:
    from bidscoin import dicomsort
except ImportError:
    import dicomsort  # This should work if bidscoin was not pip-installed
from ruamel.yaml import YAML
yaml = YAML()

logger = logging.getLogger('bidscoin')

bidsdatatypes   = ('fmap', 'anat', 'func', 'perf', 'dwi', 'meg', 'eeg', 'ieeg', 'beh', 'pet')           # NB: get_matching_run() uses this order to search for a match
ignoredatatype  = 'leave_out'
unknowndatatype = 'extra_data'
bidskeys        = ('task', 'acq', 'inv', 'mt', 'flip', 'ce', 'rec', 'dir', 'run', 'echo', 'mod', 'proc', 'part', 'suffix', 'IntendedFor') # This is not really something from BIDS, but these are the BIDS-keys used in the bidsmap

schema_folder     = Path(__file__).parents[1]/'schema'
heuristics_folder = Path(__file__).parents[1]/'heuristics'
bidsmap_template  = heuristics_folder/'bidsmap_template.yaml'


def bidsversion() -> str:
    """
    Reads the BIDS version from the BIDSVERSION.TXT file

    :return:    The BIDS version number
    """

    with (Path(__file__).parent.parent/'bidsversion.txt').open('r') as fid:
        value = fid.read().strip()

    return str(value)


def version() -> str:
    """
    Reads the BIDSCOIN version from the VERSION.TXT file

    :return:    The BIDSCOIN version number
    """

    with (Path(__file__).parent.parent/'version.txt').open('r') as fid:
        value = fid.read().strip()

    return str(value)


def setup_logging(log_file: Path=Path(), debug: bool=False) -> logging.Logger:
    """
    Setup the logging

    :param log_file:    Name of the logfile
    :param debug:       Set log level to DEBUG if debug==True
    :return:            Logger object
     """

    # debug = True

    # Set the format and logging level
    fmt       = '%(asctime)s - %(name)s - %(levelname)s %(message)s'
    datefmt   = '%Y-%m-%d %H:%M:%S'
    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)
    if debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    # Set & add the streamhandler and add some color to those boring terminal logs! :-)
    coloredlogs.install(level=logger.level, fmt=fmt, datefmt=datefmt)

    if not log_file.name:
        return

    # Set & add the log filehandler
    log_file.parent.mkdir(parents=True, exist_ok=True)      # Create the log dir if it does not exist
    loghandler = logging.FileHandler(log_file)
    loghandler.setLevel(logging.DEBUG)
    loghandler.setFormatter(formatter)
    loghandler.set_name('loghandler')
    logger.addHandler(loghandler)

    # Set & add the error / warnings handler
    error_file = log_file.with_suffix('.errors')            # Derive the name of the error logfile from the normal log_file
    errorhandler = logging.FileHandler(error_file, mode='w')
    errorhandler.setLevel(logging.WARNING)
    errorhandler.setFormatter(formatter)
    errorhandler.set_name('errorhandler')
    logger.addHandler(errorhandler)

    return logger


def reporterrors() -> None:
    """
    Summarized the warning and errors from the logfile

    :return:
    """

    for filehandler in logger.handlers:
        if filehandler.name == 'errorhandler':

            errorfile = Path(filehandler.baseFilename)
            if errorfile.stat().st_size:
                with errorfile.open('r') as fid:
                    errors = fid.read()
                logger.info(f"The following BIDScoin errors and warnings were reported:\n\n{40*'>'}\n{errors}{40*'<'}\n")

            else:
                logger.info(f'No BIDScoin errors or warnings were reported')
                logger.info('')

        elif filehandler.name == 'loghandler':
            logfile = Path(filehandler.baseFilename)

    if 'logfile' in locals():
        logger.info(f"For the complete log see: {logfile}\n"
                    f"NB: Files in {logfile.parent} may contain privacy sensitive information, e.g. pathnames in logfiles and provenance data samples")


def run_command(command: str) -> bool:
    """
    Runs a command in a shell using subprocess.run(command, ..)

    :param command: the command that is executed
    :return:        True if the were no errors, False otherwise
    """

    logger.info(f"Running: {command}")
    process = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)          # TODO: investigate shell=False and capture_output=True for python 3.7
    logger.info(f"Output:\n{process.stdout.decode('utf-8')}")

    if process.stderr.decode('utf-8') or process.returncode!=0:
        logger.exception(f"Failed to run:\n{command}\nErrorcode {process.returncode}:\n{process.stderr.decode('utf-8')}")
        logger.debug(f"{process.stdout.decode('utf-8')}")
        return False

    return True


def import_plugin(plugin: Path) -> util.module_from_spec:
    """

    :param plugin:  Name of the plugin
    :return:        plugin-module
    """

    # Get the full path to the plugin-module
    plugin = Path(plugin)
    if len(plugin.parents) == 1:
        plugin = Path(__file__).parent/'plugins'/plugin

    # See if we can find the plug-in
    if not plugin.is_file():
        logger.error(f"Could not find plugin: '{plugin}'")
        return None

    # Load the plugin-module
    try:
        spec   = util.spec_from_file_location('bidscoin_plugin', plugin)
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # bidsmapper -> module.bidsmapper_plugin(runfolder, bidsmap_new, bidsmap_old)
        if 'bidsmapper_plugin' not in dir(module):
            logger.info(f"Could not find bidscoiner_plugin() in {plugin}")

        # bidscoiner -> module.bidscoiner_plugin(session, bidsmap, bidsfolder, personals)
        if 'bidscoiner_plugin' not in dir(module):
            logger.info(f"Could not find bidscoiner_plugin() in {plugin}")

        if 'bidsmapper_plugin' not in dir(module) and 'bidscoiner_plugin' not in dir(module):
            logger.warning(f"{plugin} can (and will) not perform any operation")

        return module

    except Exception:
        logger.exception(f"Could not import '{plugin}'")

        return None


def test_tooloptions(tool: str, opts: dict) -> bool:
    """
    Performs tests of the user tool parameters set in bidsmap['Options']

    :param tool:    Name of the tool that is being tested in bidsmap['Options']
    :param opts:    The editable options belonging to the tool
    :return:        True if the tool generated the expected result, False if there was a tool error
    """

    if tool == 'dcm2niix':
        command = f"{opts['path']}dcm2niix -h"
    elif tool == 'bidsmapper':
        command = f"{Path(__file__).parent/'bidsmapper.py'} -v"
    elif tool in ('bidscoin', 'bidscoiner'):
        command = f"{Path(__file__).parent/'bidscoiner.py'} -v"
    else:
        logger.warning(f"Testing of '{tool}' not supported")
        return None

    logger.info(f"Testing: '{tool}'")

    return run_command(command)


def test_plugins(plugin: Path) -> bool:
    """
    Performs tests of the plug-ins in bidsmap['PlugIns']

    :param plugin:  The name of the plugin that is being tested (-> bidsmap['Plugins'])
    :return:        True if the plugin generated the expected result, False if there
                    was a plug-in error, None if this function has an implementation error
    """

    logger.info(f"Testing: '{plugin}' plugin")

    module = import_plugin(plugin)
    if inspect.ismodule(module):
        methods = [method for method in dir(module) if not method.startswith('_')]
        logger.info(f"Result:\n{module.__doc__}\n{plugin} attributes and methods:\n{methods}\n")
        return True

    else:
        return False


def lsdirs(folder: Path, wildcard: str='*') -> List[Path]:
    """
    Gets all directories in a folder, ignores files

    :param folder:      The full pathname of the folder
    :param wildcard:    Simple (glob.glob) shell-style wildcards. Foldernames starting with a dot are special cases that are not matched by '*' and '?' patterns.") wildcard
    :return:            A list with all directories in the folder
    """

    return [fname for fname in sorted(folder.glob(wildcard)) if fname.is_dir()]


def is_dicomfile(file: Path) -> bool:
    """
    Checks whether a file is a DICOM-file. It uses the feature that Dicoms have the string DICM hardcoded at offset 0x80.

    :param file:    The full pathname of the file
    :return:        Returns true if a file is a DICOM-file
    """

    if file.is_file():
        if file.stem.startswith('.'):
            logger.warning(f'File is hidden: {file}')
        with file.open('rb') as dicomfile:
            dicomfile.seek(0x80, 1)
            if dicomfile.read(4) == b'DICM':
                return True
            else:
                logger.debug(f"Reading non-standard DICOM file: {file}")
                dicomdata = pydicom.dcmread(file, force=True)       # The DICM tag may be missing for anonymized DICOM files
                return 'Modality' in dicomdata
    else:
        return False


def is_dicomfile_siemens(file: Path) -> bool:
    """
    Checks whether a file is a *SIEMENS* DICOM-file. All Siemens Dicoms contain a dump of the
    MrProt structure. The dump is marked with a header starting with 'ASCCONV BEGIN'. Though
    this check is not foolproof, it is very unlikely to fail.

    :param file:    The full pathname of the file
    :return:        Returns true if a file is a Siemens DICOM-file
    """

    return b'ASCCONV BEGIN' in file.open('rb').read()


def is_parfile(file: Path) -> bool:
    """
    Rudimentary check (on file extensions and whether it exists) whether a file is a Philips PAR file

    :param file:    The full pathname of the file
    :return:        Returns true if a file is a Philips PAR-file
    """

    # TODO: Implement a proper check, e.g. using nibabel
    if file.is_file() and file.suffix in ('.PAR', '.par', '.XML', '.xml'):
        return True
    else:
        return False


def is_p7file(file: Path) -> bool:
    """
    Checks whether a file is a GE P*.7 file

    WIP!!!!!!

    :param file:    The full pathname of the file
    :return:        Returns true if a file is a GE P7-file
    """

    # TODO: Returns true if filetype is P7.
    pass


def is_niftifile(file: Path) -> bool:
    """
    Checks whether a file is a nifti file

    :param file:    The full pathname of the file
    :return:        Returns true if a file is a nifti-file
    """

    # TODO: Implement a proper check, e.g. using nibabel
    if file.is_file() and file.suffix in ('.nii', '.nii.gz', '.img', '.hdr'):
        return True
    else:
        return False


def unpack(sourcefolder: Path, subprefix: str='sub-', sesprefix: str='ses-', wildcard: str='*', workfolder: Path='') -> (Path, bool):
    """
    Unpacks and sorts DICOM files in sourcefolder to a temporary folder if sourcefolder contains a DICOMDIR file or .tar.gz, .gz or .zip files

    :param sourcefolder:    The full pathname of the folder with the source data
    :param subprefix:       The optional subprefix (e.g. 'sub-'). Used to parse the subid
    :param sesprefix:       The optional sesprefix (e.g. 'ses-'). Used to parse the sesid
    :param wildcard:        A glob search pattern to select the tarballed/zipped files
    :param workfolder:      A root folder for temporary data
    :return:                A tuple with the full pathname of the source or workfolder and a workdir-path or False when the data is not unpacked in a temporary folder
    """

    # Search for zipped/tarballed files
    packedfiles = []
    packedfiles.extend(sourcefolder.glob(f"{wildcard}.tar"))
    packedfiles.extend(sourcefolder.glob(f"{wildcard}.tar.?z"))
    packedfiles.extend(sourcefolder.glob(f"{wildcard}.tar.bz2"))
    packedfiles.extend(sourcefolder.glob(f"{wildcard}.zip"))

    # Check if we are going to do unpacking and/or sorting
    if packedfiles or (sourcefolder/'DICOMDIR').is_file():

        # Create a (temporary) sub/ses workfolder for unpacking the data
        if not workfolder:
            workfolder = tempfile.mkdtemp()
        workfolder   = Path(workfolder)
        subid, sesid = get_subid_sesid(sourcefolder/'dum.my', subprefix=subprefix, sesprefix=sesprefix)
        subid, sesid = subid.replace('sub-', subprefix), sesid.replace('ses-', sesprefix)
        worksubses   = workfolder/subid/sesid
        worksubses.mkdir(parents=True, exist_ok=True)

        # Copy everything over to the workfolder
        logger.info(f"Making temporary copy: {sourcefolder} -> {worksubses}")
        copy_tree(str(sourcefolder), str(worksubses))     # Older python versions don't support PathLib

        # Unpack the zip/tarballed files in the temporary folder
        for packedfile in [worksubses/packedfile.name for packedfile in packedfiles]:
            logger.info(f"Unpacking: {packedfile.name} -> {worksubses}")
            ext = packedfile.suffixes
            if ext[-1] == '.zip':
                with zipfile.ZipFile(packedfile, 'r') as zip_fid:
                    zip_fid.extractall(worksubses)
            elif '.tar' in ext:
                with tarfile.open(packedfile, 'r') as tar_fid:
                    tar_fid.extractall(worksubses)

            # Sort the DICOM files immediately (to avoid name collisions)
            dicomsort.sortsessions(worksubses)

        # Sort the DICOM files if not sorted yet (e.g. DICOMDIR)
        dicomsort.sortsessions(worksubses)

        return worksubses, workfolder

    else:

        return sourcefolder, False


def get_dicomfile(folder: Path, index: int=0) -> Path:
    """
    Gets a dicom-file from the folder (supports DICOMDIR)

    :param folder:  The full pathname of the folder
    :param index:   The index number of the dicom file
    :return:        The filename of the first dicom-file in the folder.
    """

    if (folder/'DICOMDIR').is_file():
        dicomdir = FileSet(folder/'DICOMDIR')
        files    = [Path(file.path) for file in dicomdir]
    else:
        files = sorted(folder.iterdir())

    idx = 0
    for file in files:
        if file.stem.startswith('.'):
            logger.warning(f'Ignoring hidden file: {file}')
            continue
        if is_dicomfile(file):
            if idx == index:
                return file
            else:
                idx += 1

    return Path()


def get_parfiles(folder: Path) -> List[Path]:
    """
    Gets a Philips PAR-file from the folder

    :param folder:  The full pathname of the folder
    :return:        The filename of the first PAR-file in the folder.
    """

    parfiles = []
    for file in sorted(folder.iterdir()):
        if is_parfile(file):
            parfiles.append(file)

    return parfiles


def get_p7file(folder: Path) -> Path:
    """
    Gets a GE P*.7-file from the folder

    :param folder:  The full pathname of the folder
    :return:        The filename of the first P7-file in the folder.
    """

    pass
    return Path()


def get_niftifile(folder: Path) -> Path:
    """
    Gets a nifti-file from the folder

    :param folder:  The full pathname of the folder
    :return:        The filename of the first nifti-file in the folder.
    """

    pass
    return Path()


def load_bidsmap(yamlfile: Path, folder: Path=Path(), report: bool=True) -> Tuple[dict, Path]:
    """
    Read the mapping heuristics from the bidsmap yaml-file. If yamlfile is not fullpath, then 'folder' is first searched before
    the default 'heuristics'. If yamfile is empty, then first 'bidsmap.yaml' is searched for, them 'bidsmap_template.yaml'. So fullpath
    has precendence over folder and bidsmap.yaml has precedence over bidsmap_template.yaml

    :param yamlfile:    The full pathname or basename of the bidsmap yaml-file. If None, the default bidsmap_template.yaml file in the heuristics folder is used
    :param folder:      Only used when yamlfile=basename or None: yamlfile is then first searched for in folder and then falls back to the ./heuristics folder (useful for centrally managed template yaml-files)
    :param report:      Report log.info when reading a file
    :return:            Tuple with (1) ruamel.yaml dict structure, with all options, BIDS mapping heuristics, labels and attributes, etc and (2) the fullpath yaml-file
    """

    # Input checking
    if not folder.name:
        folder = heuristics_folder
    if not yamlfile.name:
        yamlfile = folder/'bidsmap.yaml'
        if not yamlfile.is_file():
            yamlfile = bidsmap_template

    # Add a standard file-extension if needed
    if not yamlfile.suffix:
        yamlfile = yamlfile.with_suffix('.yaml')

    # Get the full path to the bidsmap yaml-file
    if len(yamlfile.parents) == 1:
        if (folder/yamlfile).is_file():
            yamlfile = folder/yamlfile
        else:
            yamlfile = heuristics_folder/yamlfile

    if not yamlfile.is_file():
        if report:
            logger.info(f"No existing bidsmap file found: {yamlfile}")
        return dict(), yamlfile
    elif report:
        logger.info(f"Reading: {yamlfile}")

    # Read the heuristics from the bidsmap file
    with yamlfile.open('r') as stream:
        bidsmap = yaml.load(stream)

    # Issue a warning if the version in the bidsmap YAML-file is not the same as the bidscoin version
    if 'bidscoin' in bidsmap['Options'] and 'version' in bidsmap['Options']['bidscoin']:
        bidsmapversion = bidsmap['Options']['bidscoin']['version']
    elif 'version' in bidsmap['Options']:
        bidsmapversion = bidsmap['Options']['version']
    else:
        bidsmapversion = 'Unknown'

    if bidsmapversion != version() and report:
        logger.warning(f'BIDScoiner version conflict: {yamlfile} was created using version {bidsmapversion}, but this is version {version()}')

    # Make sure we get a proper list of plugins
    if not bidsmap['PlugIns']:
        bidsmap['PlugIns'] = []
    bidsmap['PlugIns'] = [plugin for plugin in bidsmap['PlugIns'] if plugin]

    return bidsmap, yamlfile


def save_bidsmap(filename: Path, bidsmap: dict) -> None:
    """
    Save the BIDSmap as a YAML text file

    :param filename:
    :param bidsmap:         Full bidsmap data structure, with all options, BIDS labels and attributes, etc
    :return:
    """

    logger.info(f"Writing bidsmap to: {filename}")
    filename.parent.mkdir(parents=True, exist_ok=True)
    with filename.open('w') as stream:
        yaml.dump(bidsmap, stream)

    # See if we can reload it, i.e. whether it is valid yaml...
    try:
        load_bidsmap(filename, report=False)
    except:
        # Just trying again seems to help? :-)
        with filename.open('w') as stream:
            yaml.dump(bidsmap, stream)
        try:
            load_bidsmap(filename, report=False)
        except:
            logger.exception(f'The saved output bidsmap does not seem to be valid YAML, please check {filename}, e.g. by way of an online yaml validator, such as https://yamlchecker.com/')


def parse_x_protocol(pattern: str, dicomfile: Path) -> str:
    """
    Siemens writes a protocol structure as text into each DICOM file.
    This structure is necessary to recreate a scanning protocol from a DICOM,
    since the DICOM information alone wouldn't be sufficient.

    :param pattern:     A regexp expression: '^' + pattern + '\t = \t(.*)\\n'
    :param dicomfile:   The full pathname of the dicom-file
    :return:            The string extracted values from the dicom-file according to the given pattern
    """

    if not is_dicomfile_siemens(dicomfile):
        logger.warning(f"Parsing {pattern} may fail because {dicomfile} does not seem to be a Siemens DICOM file")

    regexp = '^' + pattern + '\t = \t(.*)\n'
    regex  = re.compile(regexp.encode('utf-8'))

    with dicomfile.open('rb') as openfile:
        for line in openfile:
            match = regex.match(line)
            if match:
                return match.group(1).decode('utf-8')

    logger.warning(f"Pattern: '{regexp.encode('unicode_escape').decode()}' not found in: {dicomfile}")
    return None


# Profiling shows this is currently the most expensive function, so therefore the (primitive but effective) _DICOMDICT_CACHE optimization
_DICOMDICT_CACHE = None
_DICOMFILE_CACHE = None
def get_dicomfield(tagname: str, dicomfile: Path) -> Union[str, int]:
    """
    Robustly extracts a DICOM field/tag from a dictionary or from vendor specific fields

    :param tagname:     Name of the DICOM field
    :param dicomfile:   The full pathname of the dicom-file
    :return:            Extracted tag-values from the dicom-file
    """

    global _DICOMDICT_CACHE, _DICOMFILE_CACHE

    if not dicomfile.name:
        return ''

    if not dicomfile.is_file():
        logger.warning(f"{dicomfile} not found")
        value = None

    elif not is_dicomfile(dicomfile):
        logger.warning(f"{dicomfile} is not a DICOM file, cannot read {tagname}")
        value = None

    else:
        try:
            if dicomfile != _DICOMFILE_CACHE:
                dicomdata = pydicom.dcmread(dicomfile, force=True)      # The DICM tag may be missing for anonymized DICOM files
                if 'Modality' not in dicomdata:
                    raise ValueError(f'Cannot read {dicomfile}')
                _DICOMDICT_CACHE = dicomdata
                _DICOMFILE_CACHE = dicomfile
            else:
                dicomdata = _DICOMDICT_CACHE

            value = dicomdata.get(tagname)

            # Try a recursive search
            if not value:
                for elem in dicomdata.iterall():
                    if elem.name==tagname:
                        value = elem.value
                        continue

        except OSError:
            logger.warning(f'Cannot read {tagname} from {dicomfile}')
            value = None

        except Exception:
            try:
                value = parse_x_protocol(tagname, dicomfile)

            except Exception:
                logger.warning(f'Could not parse {tagname} from {dicomfile}')
                value = None

    # Cast the dicom datatype to int or str (i.e. to something that yaml.dump can handle)
    if value is None:
        return ''

    elif isinstance(value, int):
        return int(value)

    elif not isinstance(value, str):    # Assume it's a MultiValue type and flatten it
        return str(value)

    else:
        return str(value)


# Profiling shows this is currently the most expensive function, so therefore the (primitive but effective) _PARDICT_CACHE optimization
_PARDICT_CACHE = None
_PARFILE_CACHE = None
def get_parfield(tagname: str, parfile: Path) -> Union[str, int]:
    """
    Extracts the value from a PAR/XML field

    :param tagname: Name of the PAR/XML field
    :param parfile: The full pathname of the PAR/XML file
    :return:        Extracted tag-values from the PAR/XML file
    """

    global _PARDICT_CACHE, _PARFILE_CACHE

    if not parfile.name:
        return ''

    if not parfile.is_file():
        logger.warning(f"{parfile} not found")
        value = None

    elif not is_parfile(parfile):
        logger.warning(f"{parfile} is not a PAR/XML file, cannot read {tagname}")
        value = None

    else:
        try:
            if parfile != _PARFILE_CACHE:
                pardict = nibabel.parrec.parse_PAR_header(parfile.open('r'))
                if 'series_type' not in pardict[0]:
                    raise ValueError(f'Cannot read {parfile}')
                _PARDICT_CACHE = pardict
                _PARFILE_CACHE = parfile
            else:
                pardict = _PARDICT_CACHE
            value = pardict[0].get(tagname)

        except OSError:
            logger.warning(f'Cannot read {tagname} from {parfile}')
            value = None

        except Exception:
            logger.warning(f'Could not parse {tagname} from {parfile}')
            value = None

    # Cast the dicom datatype to int or str (i.e. to something that yaml.dump can handle)
    if value is None:
        return ''

    elif isinstance(value, int):
        return int(value)

    elif not isinstance(value, str):  # Assume it's a MultiValue type and flatten it
        return str(value)

    else:
        return str(value)


def get_dataformat(source: Path) -> str:
    """
    TODO: replace sourcefile with a class as soon as Pathlib supports subclassing

    :param source:  The full pathname of a (e.g. DICOM or PAR/XML) session directory or of a source file
    :return:        'DICOM' if sourcefile is a DICOM-file or 'PAR' when it is a PAR/XML file
    """


    # If source is a session directory, get a sourcefile
    try:
        if source.is_dir():

            # Try to see if we can find DICOM files
            sourcedirs = lsdirs(source)
            for sourcedir in sourcedirs:
                sourcefile = get_dicomfile(sourcedir)
                if sourcefile.name:
                    return 'DICOM'

            # Try to see if we can find PAR/XML files
            sourcefiles = get_parfiles(source)
            if sourcefiles:
                return 'PAR'

        # If we don't know the dataformat, just try
        if is_dicomfile(source):
            return 'DICOM'

        if is_parfile(source):
            return 'PAR'

    except OSError as nosource:
        logger.warning(nosource)

    logger.warning(f"Cannot determine the dataformat of: {source}")
    return ''


def get_sourcefield(tagname: str, sourcefile: Path=Path(), dataformat: str='') -> Union[str, int]:
    """
    Wrapper around get_dicomfield and get_parfield

    :param tagname:     Name of the field in the sourcefile
    :param sourcefile:  The full pathname of the (e.g. DICOM or PAR/XML) sourcefile
    :param dataformat:  The information source in the bidsmap that is used, e.g. 'DICOM'
    :return:
    """

    if not dataformat:
        dataformat = get_dataformat(sourcefile)

    if dataformat=='DICOM':
        return get_dicomfield(tagname, sourcefile)

    if dataformat=='PAR':
        return get_parfield(tagname, sourcefile)


def add_prefix(prefix: str, tag: str) -> str:
    """
    Simple function to account for optional BIDS tags in the bids file names, i.e. it prefixes 'prefix' only when tag is not empty

    :param prefix:  The prefix (e.g. '_sub-')
    :param tag:     The tag (e.g. 'control01')
    :return:        The tag with the leading prefix (e.g. '_sub-control01') or just the empty tag ''
    """

    if tag:
        tag = prefix + str(tag)
    else:
        tag = ''

    return tag


def strip_suffix(run: dict) -> dict:
    """
    Certain attributes such as SeriesDescriptions (but not ProtocolName!?) may get a suffix like '_SBRef' from the vendor,
    try to strip it off from the BIDS labels

    :param run: The run with potentially added suffixes that are the same as the BIDS suffixes
    :return:    The run with these suffixes removed
    """

    # See if we have a suffix for this datatype
    if 'suffix' in run['bids'] and run['bids']['suffix']:
        suffix = run['bids']['suffix'].lower()
    else:
        return run

    # See if any of the BIDS labels ends with the same suffix. If so, then remove it
    for key in run['bids']:
        if key == 'suffix':
            continue
        if isinstance(run['bids'][key], str) and run['bids'][key].lower().endswith(suffix):
            run['bids'][key] = run['bids'][key][0:-len(suffix)]       # NB: This will leave the added '_' and '.' characters, but they will be taken out later (as they are not BIDS-valid)

    return run


def cleanup_value(label: str) -> str:
    """
    Converts a given label to a cleaned-up label that can be used as a BIDS label. Remove leading and trailing spaces;
    convert other spaces, special BIDS characters and anything that is not an alphanumeric to a ''. This will for
    example map "Joe's reward_task" to "Joesrewardtask"

    :param label:   The given label that potentially contains undesired characters
    :return:        The cleaned-up / BIDS-valid label
    """

    if label is None:
        return ''
    if not isinstance(label, str):
        return label

    special_characters = (' ', '_', '-','.')

    for special in special_characters:
        label = label.strip().replace(special, '')

    return re.sub(r'(?u)[^-\w.]', '', label)


def dir_bidsmap(bidsmap: dict, dataformat: str) -> List[Path]:
    """
    Make a provenance list of all the runs in the bidsmap[dataformat]

    :param bidsmap:     The bidsmap, with all the runs in it
    :param dataformat:  The information source in the bidsmap that is used, e.g. 'DICOM'
    :return:            List of all provenances
    """

    provenance = []
    for datatype in bidsdatatypes + (unknowndatatype, ignoredatatype):
        if datatype in bidsmap[dataformat] and bidsmap[dataformat][datatype]:
            for run in bidsmap[dataformat][datatype]:
                if not run['provenance']:
                    logger.warning(f'The bidsmap run {datatype} run does not contain provenance data')
                else:
                    provenance.append(Path(run['provenance']))

    provenance.sort()

    return provenance


def get_run(bidsmap: dict, dataformat: str, datatype: str, suffix_idx: Union[int, str], sourcefile: Path=Path()) -> dict:
    """
    Find the (first) run in bidsmap[dataformat][bidsdatatype] with run['bids']['suffix_idx'] == suffix_idx

    :param bidsmap:     This could be a template bidsmap, with all options, BIDS labels and attributes, etc
    :param dataformat:  The information source in the bidsmap that is used, e.g. 'DICOM'
    :param datatype:    The datatype in which a matching run is searched for (e.g. 'anat')
    :param suffix_idx:  The name of the suffix that is searched for (e.g. 'bold') or the datatype index number
    :param sourcefile:  The name of the sourcefile. If given, the bidsmap values are read from file
    :return:            The clean (filled) run item in the bidsmap[dataformat][bidsdatatype] with the matching suffix_idx, otherwise a dict with empty attributes & bids keys
    """

    if not dataformat:
        dataformat = get_dataformat(sourcefile)

    for index, run in enumerate(bidsmap.get(dataformat).get(datatype,[])):
        if index == suffix_idx or run['bids']['suffix'] == suffix_idx:

            run_ = dict(provenance=str(sourcefile.resolve()), attributes={}, bids={})

            for attrkey, attrvalue in run['attributes'].items():
                if sourcefile.name:
                    run_['attributes'][attrkey] = get_sourcefield(attrkey, sourcefile, dataformat)
                else:
                    run_['attributes'][attrkey] = attrvalue

            for bidskey, bidsvalue in run['bids'].items():
                if sourcefile.name:
                    run_['bids'][bidskey] = get_dynamic_value(bidsvalue, sourcefile)
                else:
                    run_['bids'][bidskey] = bidsvalue

            return run_

    logger.warning(f"'{datatype}' run with suffix_idx '{suffix_idx}' not found in bidsmap['{dataformat}']")
    return dict(provenance=str(sourcefile.resolve()), attributes={}, bids={})


def delete_run(bidsmap: dict, dataformat: str, datatype: str, provenance: Path) -> dict:
    """
    Delete a run from the BIDS map

    :param bidsmap:     Full bidsmap data structure, with all options, BIDS labels and attributes, etc
    :param dataformat:  The information source in the bidsmap that is used, e.g. 'DICOM'
    :param datatype:    The datatype that is used, e.g. 'anat'
    :param provenance:  The unique provance that is use to identify the run
    :return:            The new bidsmap
    """

    if not dataformat:
        dataformat = get_dataformat(provenance)

    for index, run in enumerate(bidsmap[dataformat][datatype]):
        if run['provenance'] == str(provenance):
            del bidsmap[dataformat][datatype][index]

    return bidsmap


def append_run(bidsmap: dict, dataformat: str, datatype: str, run: dict, clean: bool=True) -> dict:
    """
    Append a run to the BIDS map

    :param bidsmap:     Full bidsmap data structure, with all options, BIDS labels and attributes, etc
    :param dataformat:  The information source in the bidsmap that is used, e.g. 'DICOM'. If empty then it is determined from the provenance
    :param datatype:    The datatype that is used, e.g. 'anat'
    :param run:         The run (listitem) that is appended to the datatype
    :param clean:       A boolean to clean-up commentedMap fields
    :return:            The new bidsmap
    """

    if not dataformat:
        dataformat = get_dataformat(run['provenance'])

    # Copy the values from the run to an empty dict
    if clean:
        run_ = dict(provenance='', attributes={}, bids={})

        run_['provenance'] = run['provenance']

        for key, value in run['attributes'].items():
            run_['attributes'][key] = value
        for key, value in run['bids'].items():
            run_['bids'][key] = value

        run = run_

    if not bidsmap.get(dataformat):
        bidsmap[dataformat] = {}
    elif not bidsmap.get(dataformat).get(datatype):
        bidsmap[dataformat][datatype] = [run]
    else:
        bidsmap[dataformat][datatype].append(run)

    return bidsmap


def update_bidsmap(bidsmap: dict, source_datatype: str, provenance: Path, target_datatype: str, run: dict, dataformat: str, clean: bool=True) -> dict:
    """
    Update the BIDS map if the datatype changes:
    1. Remove the source run from the source datatype section
    2. Append the (cleaned) target run to the target datatype section

    Else:
    1. Use the provenance to look-up the index number in that datatype
    2. Replace the run

    :param bidsmap:             Full bidsmap data structure, with all options, BIDS labels and attributes, etc
    :param source_datatype:     The current datatype name, e.g. 'anat'
    :param provenance:          The unique provance that is use to identify the run
    :param target_datatype:     The datatype name what is should be, e.g. 'dwi'
    :param run:                 The run item that is being moved
    :param dataformat:          The name of the dataformat, e.g. 'DICOM'
    :param clean:               A boolean that is passed to bids.append_run (telling it to clean-up commentedMap fields)
    :return:
    """

    if not dataformat:
        dataformat = get_dataformat(run['provenance'])

    num_runs_in = len(dir_bidsmap(bidsmap, dataformat))

    # Warn the user if the target run already exists when the run is moved to another datatype
    if source_datatype != target_datatype:
        if exist_run(bidsmap, dataformat, target_datatype, run):
            logger.warning(f'That run from {source_datatype} already exists in {target_datatype}...')

        # Delete the source run
        bidsmap = delete_run(bidsmap, dataformat, source_datatype, provenance)

        # Append the (cleaned-up) target run
        bidsmap = append_run(bidsmap, dataformat, target_datatype, run, clean)

    else:
        for index, run_ in enumerate(bidsmap[dataformat][target_datatype]):
            if run_['provenance'] == str(provenance):
                bidsmap[dataformat][target_datatype][index] = run
                break

    num_runs_out = len(dir_bidsmap(bidsmap, dataformat))
    if num_runs_out != num_runs_in:
        logger.exception(f"Number of runs in bidsmap['{dataformat}'] changed unexpectedly: {num_runs_in} -> {num_runs_out}")

    return bidsmap


def match_attribute(longvalue, values) -> bool:
    """
    Compare the value items with / without *wildcard* with the longvalue string. If both longvalue
    and values are a list then they are directly compared as is

    Examples:
        match_attribute('my_pulse_sequence_name', 'name') -> False
        match_attribute('my_pulse_sequence_name', '*name*') -> True
        match_attribute('T1_MPRAGE', '['T1w', 'MPRAGE']') -> False
        match_attribute('T1_MPRAGE', '['T1w', 'T1_MPRAGE']') -> True
        match_attribute('T1_MPRAGE', '['*T1w*', '*MPRAGE*']') -> True

    :param longvalue:   The long string that is being searched in (e.g. a DICOM attribute)
    :param values:      Either a list with search items or a string that is matched one-to-one
    :return:            True if a match is found or both longvalue and values are identical or
                        empty / None. False otherwise
    """

    # Consider it a match if both longvalue and values are identical or empty / None
    if longvalue==values or (not longvalue and not values):
        return True

    if not longvalue or not values:
        return False

    # Make sure we start with string types
    longvalue = str(longvalue)
    values    = str(values)

    # Interpret attribute lists as lists
    def cast2list(string: str):
        if string.startswith('[') and string.endswith(']'):
            try:
                string = ast.literal_eval(string)
                if not isinstance(string, list):
                    logger.error(f"Attribute value '{string}' is not a list")
            except:
                logger.error(f"Could not interpret attribute value '{string}'")
        return string

    longvalue = cast2list(longvalue)
    values    = cast2list(values)

    # Account for lists in the template (to combine similar mappings)
    if not isinstance(values, list):
        values = [values]

    # If they are both lists, compare them as they are
    elif isinstance(longvalue, list):
        return str(longvalue)==str(values)

    # Compare the value items (with / without wildcard) with the longvalue string items
    if not isinstance(longvalue, list):
        longvalue = [longvalue]
    for value in values:
        if any([fnmatch.fnmatchcase(str(item), str(value)) for item in longvalue]):
            return True

    return False


def exist_run(bidsmap: dict, dataformat: str, datatype: str, run_item: dict, matchbidslabels: bool=False) -> bool:
    """
    Checks if there is already an entry in runlist with the same attributes and, optionally, bids values as in the input run

    :param bidsmap:         Full bidsmap data structure, with all options, BIDS labels and attributes, etc
    :param dataformat:      The information source in the bidsmap that is used, e.g. 'DICOM'
    :param datatype:        The datatype in the source that is used, e.g. 'anat'. Empty values will search through all datatypes
    :param run_item:        The run (listitem) that is searched for in the datatype
    :param matchbidslabels: If True, also matches the BIDS-keys, otherwise only run['attributes']
    :return:                True if the run exists in runlist
    """

    if not dataformat:
        dataformat = get_dataformat(run_item['provenance'])

    if not datatype:
        for datatype in bidsdatatypes + (unknowndatatype, ignoredatatype):
            if exist_run(bidsmap, dataformat, datatype, run_item, matchbidslabels):
                return True

    if not bidsmap.get(dataformat).get(datatype):
        return False

    for run in bidsmap[dataformat][datatype]:

        # Begin with match = False only if all attributes are empty
        match = any([run_item['attributes'][key] is not None for key in run_item['attributes']])

        # Search for a case where all run_item items match with the run_item items
        for itemkey, itemvalue in run_item['attributes'].items():
            value = run['attributes'].get(itemkey, None)    # Matching bids-labels which exist in one datatype but not in the other -> None
            match = match and match_attribute(itemvalue, value)
            if not match:
                break                                       # There is no point in searching further within the run_item now that we've found a mismatch

        # See if the bidskeys also all match. This is probably not very useful, but maybe one day...
        if matchbidslabels and match:
            for itemkey, itemvalue in run_item['bids'].items():
                value = run['bids'].get(itemkey, None)      # Matching bids-labels which exist in one datatype but not in the other -> None
                match = match and value==itemvalue
                if not match:
                    break                                   # There is no point in searching further within the run_item now that we've found a mismatch

        # Stop searching if we found a matching run_item (i.e. which is the case if match is still True after all run tests). TODO: maybe count how many instances, could perhaps be useful info
        if match:
            return True

    return False


def check_run(datatype: str, run_item: dict) -> bool:
    """
    Check run for required and optional entitities using the BIDS schema files

    :param datatype:        The datatype that is checked, e.g. 'anat'
    :param run_item:        The run (listitem) with bids entities that are checked against missing values & invalid keys
    :return:                False if an inconsistency was found, otherwise True
    """

    run_ok = True
    found  = False

    # Read the entities from the datatype file
    datatypefile = schema_folder/'datatypes'/f"{datatype}.yaml"
    if not datatypefile.is_file():
        if datatype in bidsdatatypes:
            logger.warning(f"Could not find {datatypefile}")
        return True
    with datatypefile.open('r') as stream:
        groups = yaml.load(stream)
    for group in groups:
        if run_item['bids']['suffix'] in group['suffixes']:
            # Check if all the entities are ok
            found = True
            for entity in group['entities']:
                if entity in ('sub', 'ses'): continue
                if group['entities'][entity]=='required' and not run_item['bids'].get(entity):
                    logger.info(f'Run entity "{entity}" is required for {datatype}/*_{run_item["bids"]["suffix"]}')
                    run_ok = False
            for entity in run_item['bids']:
                if entity in ('suffix', 'IntendedFor'): continue
                if entity not in group['entities'] and run_item["bids"][entity]:
                    logger.info(f'Run entity "{entity}"-"{run_item["bids"][entity]}" is not allowed according to the BIDS standard (clear "{run_item["bids"][entity]})" to resolve this issue)')
                    run_ok = False

    return found and run_ok


def get_matching_run(sourcefile: Path, bidsmap: dict, dataformat: str, datatypes: tuple = (ignoredatatype,) + bidsdatatypes + (unknowndatatype,)) -> Tuple[dict, str, Union[int, None]]:
    """
    Find the first run in the bidsmap with dicom attributes that match with the dicom file. Then update the (dynamic) bids values (values are cleaned-up to be BIDS-valid)

    :param sourcefile:  The full pathname of the source dicom-file or PAR/XML file
    :param bidsmap:     Full bidsmap data structure, with all options, BIDS keys and attributes, etc
    :param dataformat:  The information source in the bidsmap that is used, e.g. 'DICOM'
    :param datatypes:   The datatypes in which a matching run is searched for. Default = (ignoredatatype,) + bidsdatatypes + (unknowndatatype,)
    :return:            (run, datatype, index) The matching and filled-in / cleaned run item, datatype and list index as in run = bidsmap[DICOM][datatype][index]
                        datatype = bids.unknowndatatype and index = None if there is no match, the run is still populated with info from the dicom-file
    """

    if not dataformat:
        dataformat = get_dataformat(sourcefile)

    # Loop through all bidsdatatypes and runs; all info goes into run_
    run_ = dict(provenance='', attributes={}, bids={})
    for datatype in datatypes:

        if bidsmap.get(dataformat).get(datatype) is None: continue

        for index, run in enumerate(bidsmap[dataformat][datatype]):

            run_  = dict(provenance='', attributes={}, bids={})                                             # The CommentedMap API is not guaranteed for the future so keep this line as an alternative
            match = any([run['attributes'][attrkey] is not None for attrkey in run['attributes']])          # Normally match==True, but make match==False if all attributes are empty

            # Try to see if the sourcefile matches all of the attributes and fill all of them
            for attrkey, attrvalue in run['attributes'].items():

                # Check if the attribute value matches with the info from the sourcefile
                sourcevalue = get_sourcefield(attrkey, sourcefile, dataformat)
                if attrvalue:
                    match = match and match_attribute(sourcevalue, attrvalue)

                # Fill the empty attribute with the info from the sourcefile
                run_['attributes'][attrkey] = sourcevalue

            # Try to fill the bids-labels
            for bidskey, bidsvalue in run['bids'].items():

                # Replace the dynamic bids values
                run_['bids'][bidskey] = get_dynamic_value(bidsvalue, sourcefile)

                # SeriesDescriptions (and ProtocolName?) may get a suffix like '_SBRef' from the vendor, try to strip it off
                run_ = strip_suffix(run_)

            # Stop searching the bidsmap if we have a match. TODO: check if there are more matches (i.e. conflicts)
            if match:
                run_['provenance'] = str(sourcefile.resolve())

                return run_, datatype, index

    # We don't have a match (all tests failed, so datatype should be the *last* one, i.e. unknowndatatype)
    logger.debug(f"Could not find a matching run in the bidsmap for {sourcefile} -> {datatype}")
    run_['provenance'] = str(sourcefile.resolve())

    return run_, datatype, None


def get_subid_sesid(sourcefile: Path, subid: str= '<<SourceFilePath>>', sesid: str= '<<SourceFilePath>>', subprefix: str= 'sub-', sesprefix: str= 'ses-') -> Tuple[str, str]:
    """
    Extract the cleaned-up subid and sesid from the pathname if subid/sesid == '<<SourceFilePath>>', or from the dicom header

    :param sourcefile: The full pathname of the file. If it is a DICOM file, the sub/ses values are read from the DICOM field
    :param subid:      The subject identifier, i.e. name of the subject folder (e.g. 'sub-001' or just '001') or DICOM field. Can be left empty
    :param sesid:      The optional session identifier, i.e. name of the session folder (e.g. 'ses-01' or just '01') or DICOM field
    :param subprefix:  The optional subprefix (e.g. 'sub-'). Used to parse the sub-value from the provenance as default subid
    :param sesprefix:  The optional sesprefix (e.g. 'ses-'). If it is found in the provenance then a default sesid will be set
    :return:           Updated (subid, sesid) tuple, including the BIDS-compliant sub-/ses-prefix
    """

    # Input checking
    if subprefix not in str(sourcefile):
        logger.warning(f"Could not parse sub/ses-id information from '{sourcefile}': no '{subprefix}' label in its path")
        return '', ''

    # Add default value for subid and sesid (e.g. for the bidseditor)
    if subid=='<<SourceFilePath>>':
        subid = [part for part in sourcefile.parent.parts if part.startswith(subprefix)][-1]
    else:
        subid = get_dynamic_value(subid, sourcefile)
    if sesid=='<<SourceFilePath>>':
        sesid = [part for part in sourcefile.parent.parts if part.startswith(sesprefix)]
        if sesid:
            sesid = sesid[-1]
        else:
            sesid = ''
    else:
        sesid = get_dynamic_value(sesid, sourcefile)

    # Add sub- and ses- prefixes if they are not there
    subid = 'sub-' + cleanup_value(re.sub(f'^{subprefix}', '', subid))
    if sesid:
        sesid = 'ses-' + cleanup_value(re.sub(f'^{sesprefix}', '', sesid))

    return subid, sesid


def get_derivatives() -> list:
    """
    Retrieves a list of suffixes that are stored in the derivatives folder (e.g. the qMRI maps). TODO: Replace with a more systematic / documented method
    """

    with (schema_folder/'datatypes'/'anat.yaml').open('r') as stream:
        groups      = yaml.load(stream)
        return groups[1]['suffixes']            # The qMRI data (maps)


def get_bidsname(subid: str, sesid: str, datatype: str, run: dict, runindex: str= '', subprefix: str= 'sub-', sesprefix: str= 'ses-') -> str:
    """
    Composes a filename as it should be according to the BIDS standard using the BIDS keys in run

    :param subid:       The subject identifier, i.e. name of the subject folder (e.g. 'sub-001' or just '001'). Can be left empty
    :param sesid:       The optional session identifier, i.e. name of the session folder (e.g. 'ses-01' or just '01'). Can be left empty
    :param datatype:    The bids datatype (choose from bids.bidsdatatypes)
    :param run:         The run mapping with the BIDS key-value pairs
    :param runindex:    The optional runindex label (e.g. 'run-01'). Can be left ''
    :param subprefix:   The optional subprefix (e.g. 'sub-'). Used to parse the sub-value from the provenance as default subid
    :param sesprefix:   The optional sesprefix (e.g. 'ses-'). If it is found in the provenance then a default sesid will be set
    :return:            The composed BIDS file-name (without file-extension)
    """

    assert datatype in bidsdatatypes + (unknowndatatype, ignoredatatype)

    # Try to update the sub/ses-ids
    if run['provenance']:
        subid, sesid = get_subid_sesid(Path(run['provenance']), subid, sesid, subprefix, sesprefix)

    # Validate and do some checks to allow for dragging the run entries between the different datatype-sections
    run = copy.deepcopy(run)                # Avoid side effects when changing run
    for bidskey in bidskeys:
        bidsvalue = run['bids'].get(bidskey, '')
        if isinstance(bidsvalue, list):
            bidsvalue = bidsvalue[bidsvalue[-1]]    # Get the selected item
        else:
            bidsvalue = cleanup_value(get_dynamic_value(bidsvalue, Path(run['provenance'])))
        run['bids'][bidskey] = bidsvalue

    # Use the clean-up runindex
    if not runindex:
        runindex = run['bids']['run']

    # Compose the BIDS filename (-> switch statement)
    if datatype == 'anat':

        # bidsname: sub-<label>[_ses-<label>][_acq-<label>][_ce-<label>][_rec-<label>][_run-<index>][_part-<label>]_<modality_label>
        bidsname = '{sub}{_ses}{_acq}{_inv}{_ce}{_rec}{_run}{_mod}{_part}_{suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            _acq    = add_prefix('_acq-',  run['bids']['acq']),
            _inv    = add_prefix('_inv-',  run['bids']['inv']),
            _ce     = add_prefix('_ce-',   run['bids']['ce']),
            _rec    = add_prefix('_rec-',  run['bids']['rec']),
            _run    = add_prefix('_run-',  runindex),
            _mod    = add_prefix('_mod-',  run['bids']['mod']),
            _part   = add_prefix('_part-', run['bids']['part']),
            suffix  = run['bids']['suffix'])

    elif datatype == 'func':

        # bidsname: sub-<label>[_ses-<label>]_task-<label>[_acq-<label>][_ce-<label>][_dir-<label>][_rec-<label>][_run-<index>][_echo-<index>]_<contrast_label>.nii[.gz]
        bidsname = '{sub}{_ses}_{task}{_acq}{_ce}{_dir}{_rec}{_run}{_echo}_{suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            task    = f"task-{run['bids']['task']}",
            _acq    = add_prefix('_acq-',  run['bids']['acq']),
            _ce     = add_prefix('_ce-',   run['bids']['ce']),
            _dir    = add_prefix('_dir-',  run['bids']['dir']),
            _rec    = add_prefix('_rec-',  run['bids']['rec']),
            _run    = add_prefix('_run-',  runindex),
            _echo   = add_prefix('_echo-', run['bids']['echo']),
            suffix  = run['bids']['suffix'])

    elif datatype == 'perf':

        # bidsname: sub-<label>[_ses-<label>][_acq-<label>][_rec-<label>][_dir-<label>][_run-<index>]_<perf_label>.nii[.gz]
        bidsname = '{sub}{_ses}{_acq}{_rec}{_dir}{_run}_{suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            _acq    = add_prefix('_acq-',  run['bids']['acq']),
            _rec    = add_prefix('_rec-',  run['bids']['rec']),
            _dir    = add_prefix('_dir-',  run['bids']['dir']),
            _run    = add_prefix('_run-',  runindex),
            suffix  = run['bids']['suffix'])

    elif datatype == 'dwi':

        # bidsname: sub-<label>[_ses-<label>][_acq-<label>][_dir-<label>][_run-<index>]_dwi.nii[.gz]
        bidsname = '{sub}{_ses}{_acq}{_dir}{_run}_{suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            _acq    = add_prefix('_acq-', run['bids']['acq']),
            _dir    = add_prefix('_dir-', run['bids']['dir']),
            _run    = add_prefix('_run-', runindex),
            suffix  = run['bids']['suffix'])

    elif datatype == 'fmap':

        # TODO: add more fieldmap logic?

        # bidsname: sub-<label>[_ses-<label>][_acq-<label>][_ce-<label>]_dir-<label>[_run-<index>]_epi.nii[.gz]
        bidsname = '{sub}{_ses}{_acq}{_ce}{_dir}{_run}_{suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            _acq    = add_prefix('_acq-', run['bids']['acq']),
            _ce     = add_prefix('_ce-',  run['bids']['ce']),
            _dir    = add_prefix('_dir-', run['bids']['dir']),
            _run    = add_prefix('_run-', runindex),
            suffix  = run['bids']['suffix'])

    elif datatype == 'meg':

        # bidsname: sub-<label>[_ses-<label>]_task-<label>[_acq-<label>][_run-<index>][_proc-<label>]_meg.<manufacturer_specific_extension>
        bidsname = '{sub}{_ses}_{task}{_acq}{_proc}{_run}_{suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            task    = f"task-{run['bids']['task']}",
            _acq    = add_prefix('_acq-', run['bids']['acq']),
            _proc   = add_prefix('_proc-', run['bids']['proc']),
            _run    = add_prefix('_run-', runindex),
            suffix  = run['bids']['suffix'])

    elif datatype == 'eeg':
        # bidsname: sub-<label>[_ses-<label>]_task-<label>[_acq-<label>][_run-<index>]_eeg.<manufacturer_specific_extension>
        bidsname = '{sub}{_ses}_{task}{_acq}{_run}_{suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            task    = f"task-{run['bids']['task']}",
            _acq    = add_prefix('_acq-', run['bids']['acq']),
            _run    = add_prefix('_run-', runindex),
            suffix  = run['bids']['suffix'])

    elif datatype == 'ieeg':
        # bidsname: sub-<label>[_ses-<label>]_task-<label>[_acq-<label>][_run-<index>]_ieeg.<manufacturer_specific_extension>
        bidsname = '{sub}{_ses}_{task}{_acq}{_run}_{suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            task    = f"task-{run['bids']['task']}",
            _acq    = add_prefix('_acq-', run['bids']['acq']),
            _run    = add_prefix('_run-', runindex),
            suffix  = run['bids']['suffix'])

    elif datatype == 'beh':

        # bidsname: sub-<label>[_ses-<label>]_task-<task_name>_suffix
        bidsname = '{sub}{_ses}_{task}_{suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            task    = f"task-{run['bids']['task']}",
            suffix  = run['bids']['suffix'])

    elif datatype == 'pet':

        # bidsname: sub-<label>[_ses-<label>][_task-<task_label>][_acq-<label>][_rec-<label>][_run-<index>]_suffix
        bidsname = '{sub}{_ses}{_task}{_acq}{_rec}{_run}_{suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            _task   = add_prefix('_task-', run['bids']['task']),
            _acq    = add_prefix('_acq-',  run['bids']['acq']),
            _rec    = add_prefix('_rec-',  run['bids']['rec']),
            _run    = add_prefix('_run-',  runindex),
            suffix  = run['bids']['suffix'])

    elif datatype == unknowndatatype or datatype == ignoredatatype:

        # bidsname: sub-<label>[_ses-<label>]_acq-<label>[..][_suffix]
        bidsname = '{sub}{_ses}{_task}_{acq}{_ce}{_rec}{_dir}{_run}{_echo}{_mod}{_suffix}'.format(
            sub     = subid,
            _ses    = add_prefix('_', sesid),
            _task   = add_prefix('_task-', run['bids']['task']),
            acq     = f"acq-{run['bids']['acq']}",
            _ce     = add_prefix('_ce-',   run['bids']['ce']),
            _rec    = add_prefix('_rec-',  run['bids']['rec']),
            _dir    = add_prefix('_dir-',  run['bids']['dir']),
            _run    = add_prefix('_run-',  runindex),
            _echo   = add_prefix('_echo-', run['bids']['echo']),
            _mod    = add_prefix('_mod-',  run['bids']['mod']),
            _suffix = add_prefix('_',      run['bids']['suffix']))

    else:
        logger.exception(f'Critical error: datatype "{datatype}" not implemented, please inform the developers about this error')
        raise ValueError()

    return bidsname


def get_bidshelp(entity: str) -> str:
    """
    Reads the meta-data of a matching entity in the heuristics/entities.yaml file

    :param entity:
    :return:
    """

    # Read the heuristics from the bidsmap file
    yamlfile = schema_folder/'entities.yaml'
    with yamlfile.open('r') as stream:
        entities = yaml.load(stream)
    for item in entities:
        if entities[item]['entity'] == entity:
            return f"{entities[item]['name']}\n{entities[item]['description']}"
    return ''


def get_dynamic_value(bidsvalue: str, sourcefile: Path) -> str:
    """
    Replaces (dynamic) bidsvalues with (DICOM) run attributes when they start with '<' and end with '>',
    but not with '<<' and '>>'

    :param bidsvalue:   The value from the BIDS key-value pair
    :param sourcefile:  The source (e.g. DICOM or PAR/XML) file from which the attribute is read
    :return:            Updated bidsvalue (if possible, otherwise the original bidsvalue is returned)
    """

    # Intelligent filling of the value is done runtime by bidscoiner
    if not bidsvalue or not isinstance(bidsvalue, str) or bidsvalue.startswith('<<') and bidsvalue.endswith('>>'):
        return bidsvalue

    # Fill any bids-key with the <annotated> dicom attribute(s)
    if bidsvalue.startswith('<') and bidsvalue.endswith('>') and sourcefile.name:
        sourcevalue = ''.join([str(get_sourcefield(value, sourcefile)) for value in bidsvalue[1:-1].split('><')])
        if not sourcevalue:
            return bidsvalue
        else:
            bidsvalue = cleanup_value(str(sourcevalue))

    return bidsvalue


def get_bidsvalue(bidsfile: Union[str, Path], bidskey: str, newvalue: str= '') -> Union[Path, str]:
    """
    Sets the bidslabel, i.e. '*_bidskey-*_' is replaced with '*_bidskey-bidsvalue_'. If the key is not in the bidsname
    then the newvalue is appended to the acquisition label. If newvalue is empty (= default), then the parsed existing
    bidsvalue is returned and nothing is set

    :param bidsfile:    The bidsname (e.g. as returned from get_bidsname or fullpath)
    :param bidskey:     The name of the bidskey, e.g. 'echo' or 'suffix'
    :param newvalue:    The new bidsvalue. NB: remove non-BIDS compliant characters beforehand (e.g. using cleanup_value)
    :return:            The bidsname with the new bidsvalue or, if newvalue is empty, the existing bidsvalue
    """

    bidspath = Path(bidsfile).parent
    bidsname = Path(bidsfile).with_suffix('').stem
    bidsext  = ''.join(Path(bidsfile).suffixes)

    # Get the existing bidsvalue
    oldvalue = ''
    acqvalue = ''
    if bidskey == 'suffix':
        oldvalue = bidsname.split('_')[-1]
    else:
        for entity in bidsname.split('_'):
            if '-' in entity:
                key, value = entity.split('-', 1)
                if key==bidskey:
                    oldvalue = value
                if key=='acq':
                    acqvalue = value

    # Replace the existing bidsvalue with the new value or append the newvalue to the acquisition value
    if newvalue:
        if f'_{bidskey}-' not in bidsname + 'suffix':
            if '_acq-' not in bidsname:         # Insert the 'acq' key right after the sub/ses key-value pairs
                keyval = bidsname.split('_')
                if get_bidsvalue(bidsname, 'ses'):
                    keyval.insert(2, 'acq-')
                else:
                    keyval.insert(1, 'acq-')
                bidsname = '_'.join(keyval)
            bidskey  = 'acq'
            oldvalue = acqvalue
            newvalue = acqvalue + newvalue

        # Return the updated bidsfile
        if bidskey == 'suffix':
            newbidsfile = (bidspath/(bidsname.replace(f'_{oldvalue}', f'_{newvalue}'))).with_suffix(bidsext)
        else:
            newbidsfile = (bidspath/(bidsname.replace(f'{bidskey}-{oldvalue}', f'{bidskey}-{newvalue}'))).with_suffix(bidsext)
        if isinstance(bidsfile, str):
            newbidsfile = str(newbidsfile)
        return newbidsfile

    # Or just return the parsed old bidsvalue
    else:
        return oldvalue


def increment_runindex(bidsfolder: Path, bidsname: str, ext: str='.*') -> Union[Path, str]:
    """
    Checks if a file with the same the bidsname already exists in the folder and then increments the runindex (if any)
    until no such file is found

    :param bidsfolder:  The full pathname of the bidsfolder
    :param bidsname:    The bidsname with a provisional runindex
    :param ext:         The file extension for which the runindex is incremented (default = '.*')
    :return:            The bidsname with the incremented runindex
    """

    while list(bidsfolder.glob(bidsname + ext)):

        runindex = get_bidsvalue(bidsname, 'run')
        if runindex:
            bidsname = get_bidsvalue(bidsname, 'run', str(int(runindex) + 1))

    return bidsname
