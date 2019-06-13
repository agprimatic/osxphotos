import platform
import os.path
from pathlib import Path
from plistlib import load as plistload
from datetime import datetime
import tempfile
import objc
import CoreFoundation
from Foundation import *
import urllib.parse
import sys
from shutil import copyfile
import pprint
import sqlite3
from loguru import logger
from . import _applescript

# replace string formatting with fstrings

_debug = True 

def _get_os_version():
    # returns tuple containing OS version
    # e.g. 10.13.6 = (10, 13, 6)
    (ver, major, minor) = platform.mac_ver()[0].split(".")
    return (ver, major, minor)


def _check_file_exists(filename):
    # returns true if file exists and is not a directory
    # otherwise returns false
    filename = os.path.abspath(filename)
    return os.path.exists(filename) and not os.path.isdir(filename)


class PhotosDB:
    def __init__(self, dbfile=None):
        # Dict with information about all photos by uuid
        self._dbphotos = {}
        # Dict with information about all persons/photos by uuid
        self._dbfaces_uuid = {}
        # Dict with information about all persons/photos by person
        self._dbfaces_person = {}
        # Dict with information about all keywords/photos by uuid
        self._dbkeywords_uuid = {}
        # Dict with information about all keywords/photos by keyword
        self._dbkeywords_keyword = {}
        # Dict with information about all albums/photos by uuid
        self._dbalbums_uuid = {}
        # Dict with information about all albums/photos by album
        self._dbalbums_album = {}
        # Dict with information about all the volumes/photos by uuid
        self._dbvolumes = {}

        print(dbfile)
        if dbfile is None:
            library_path = self.get_photos_library_path()
            print("library_path: " + library_path)
            # TODO: verify library path not None
            dbfile = os.path.join(library_path, "database/photos.db")
            print(dbfile)

        logger.debug("filename = %s" % dbfile)

        # TODO: replace os.path with pathlib
        # TODO: clean this up -- we'll already know library_path
        library_path = os.path.dirname(dbfile)
        (library_path, tmp) = os.path.split(library_path)
        masters_path = os.path.join(library_path, "Masters")
        self._masters_path = masters_path
        logger.debug("library = %s, masters = %s" % (library_path, masters_path))

        if not _check_file_exists(dbfile):
            sys.exit("_dbfile %s does not exist" % (dbfile))

        logger.info("database filename = %s" % dbfile)

        self._dbfile = dbfile
        self._setup_applescript()
        self._process_database()

    def keywords_as_dict(self):
        # return keywords as dict of keyword, count in reverse sorted order (descending)
        keywords = {}
        for k in self._dbkeywords_keyword.keys():
            keywords[k] = len(self._dbkeywords_keyword[k])
        keywords = dict(sorted(keywords.items(), key=lambda kv: kv[1], reverse=True))
        return keywords

    def persons_as_dict(self):
        # return persons as dict of person, count in reverse sorted order (descending)
        persons = {}
        for k in self._dbfaces_person.keys():
            persons[k] = len(self._dbfaces_person[k])
        persons = dict(sorted(persons.items(), key=lambda kv: kv[1], reverse=True))
        return persons 

    def albums_as_dict(self):
        # return albums as dict of albums, count in reverse sorted order (descending)
        albums= {}
        for k in self._dbalbums_album.keys():
            albums[k] = len(self._dbalbums_album[k])
        albums = dict(sorted(albums.items(), key=lambda kv: kv[1], reverse=True))
        return albums 

    def keywords(self):
        # return list of keywords found in photos database
        keywords = self._dbkeywords_keyword.keys()
        return list(keywords)

    def persons(self):
        # return persons as dict of person, count in reverse sorted order (descending)
        persons = self._dbfaces_person.keys()
        return list(persons)

    def albums(self):
        # return albums as dict of albums, count in reverse sorted order (descending)
        albums= self._dbalbums_album.keys()
        return list(albums)


    # Various AppleScripts we need
    def _setup_applescript(self):
        self._scpt_export = ""
        self._scpt_launch = ""
        self._scpt_quit = ""

        # Compile apple script that exports one image
        #          self._scpt_export = _applescript.AppleScript('''
        #  on run {arg}
        #  set thepath to "%s"
        #  tell application "Photos"
        #  set theitem to media item id arg
        #  set thelist to {theitem}
        #  export thelist to POSIX file thepath
        #  end tell
        #  end run
        #  ''' % (tmppath))
        #
        # Compile apple script that launches Photos.App
        self._scpt_launch = _applescript.AppleScript(
            """
            on run
              tell application "Photos"
                activate
              end tell
            end run
            """
        )

        # Compile apple script that quits Photos.App
        self._scpt_quit = _applescript.AppleScript(
            """
            on run
              tell application "Photos"
                quit
              end tell
            end run
            """
        )

    def get_photos_library_path(self):
        # return the path to the Photos library
        plist_file = Path(
            str(Path.home())
            + "/Library/Containers/com.apple.Photos/Data/Library/Preferences/com.apple.Photos.plist"
        )
        if plist_file.is_file():
            with open(plist_file, "rb") as fp:
                pl = plistload(fp)
        else:
            print("could not find plist file: " + str(plist_file), file=sys.stderr)
            return None

        # get the IPXDefaultLibraryURLBookmark from com.apple.Photos.plist
        # this is a serialized CFData object
        photosurlref = pl["IPXDefaultLibraryURLBookmark"]

        if photosurlref != None:
            # use CFURLCreateByResolvingBookmarkData to de-serialize bookmark data into a CFURLRef
            photosurl = CoreFoundation.CFURLCreateByResolvingBookmarkData(
                kCFAllocatorDefault, photosurlref, 0, None, None, None, None
            )

            # the CFURLRef we got is a sruct that python treats as an array
            # I'd like to pass this to CFURLGetFileSystemRepresentation to get the path but
            # CFURLGetFileSystemRepresentation barfs when it gets an array from python instead of expected struct
            # first element is the path string in form:
            # file:///Users/username/Pictures/Photos%20Library.photoslibrary/
            photosurlstr = photosurl[0].absoluteString() if photosurl[0] else None

            # now coerce the file URI back into an OS path
            # surely there must be a better way
            if photosurlstr is not None:
                photospath = os.path.normpath(
                    urllib.parse.unquote(urllib.parse.urlparse(photosurlstr).path)
                )
            else:
                print(
                    "Could not extract photos URL String from IPXDefaultLibraryURLBookmark",
                    file=sys.stderr,
                )
                return None

            return photospath
        else:
            print("Could not get path to Photos database", file=sys.stderr)
            return None

    def _copy_db_file(self, fname):
        # copies the sqlite database file to a temp file
        # returns the name of the temp file
        # required because python's sqlite3 implementation can't read a locked file
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="photos")
        logger.debug("copying " + fname + " to " + tmp)
        try:
            copyfile(fname, tmp)
        except:
            print("copying " + fname + " to " + tmp, file=sys.stderr)
            sys.exit()
        return tmp

    def _open_sql_file(self, file):
        fname = file
        logger.debug("Trying to open database %s" % (fname))
        try:
            conn = sqlite3.connect("%s" % (fname))
            c = conn.cursor()
        except sqlite3.Error as e:
            print("An error occurred: %s %s" % (e.args[0], fname))
            sys.exit(3)
        logger.debug("SQLite database is open")
        return (conn, c)

    def _process_database(self):
        global _debug

        fname = self._dbfile

        # Epoch is Jan 1, 2001
        td = (datetime(2001, 1, 1, 0, 0) - datetime(1970, 1, 1, 0, 0)).total_seconds()

        # Ensure Photos.App is not running
        self._scpt_quit.run()

        tmp_db = self._copy_db_file(fname)
        (conn, c) = self._open_sql_file(tmp_db)
        logger.debug("Have connection with database")

        # Look for all combinations of persons and pictures
        logger.debug("Getting information about persons")

        i = 0
        c.execute(
            "select count(*) from RKFace, RKPerson where RKFace.personID = RKperson.modelID"
        )
        # init_pbar_status("Faces", c.fetchone()[0])
        # c.execute("select RKPerson.name, RKFace.imageID from RKFace, RKPerson where RKFace.personID = RKperson.modelID")
        c.execute(
            "select RKPerson.name, RKVersion.uuid from RKFace, RKPerson, RKVersion, RKMaster "
            + "where RKFace.personID = RKperson.modelID and RKVersion.modelId = RKFace.ImageModelId "
            + "and RKVersion.type = 2 and RKVersion.masterUuid = RKMaster.uuid and "
            + "RKVersion.filename not like '%.pdf'"
        )
        for person in c:
            if person[0] == None:
                logger.debug("skipping person = None %s" % person[1])
                continue
            if not person[1] in self._dbfaces_uuid:
                self._dbfaces_uuid[person[1]] = []
            if not person[0] in self._dbfaces_person:
                self._dbfaces_person[person[0]] = []
            self._dbfaces_uuid[person[1]].append(person[0])
            self._dbfaces_person[person[0]].append(person[1])
            #  set_pbar_status(i)
            i = i + 1
        logger.debug("Finished walking through persons")
        #  close_pbar_status()

        logger.debug("Getting information about albums")
        i = 0
        c.execute(
            "select count(*) from RKAlbum, RKVersion, RKAlbumVersion where "
            + "RKAlbum.modelID = RKAlbumVersion.albumId and "
            + "RKAlbumVersion.versionID = RKVersion.modelId and "
            + "RKVersion.filename not like '%.pdf' and RKVersion.isInTrash = 0"
        )
        #  init_pbar_status("Albums", c.fetchone()[0])
        # c.execute("select RKPerson.name, RKFace.imageID from RKFace, RKPerson where RKFace.personID = RKperson.modelID")
        c.execute(
            "select RKAlbum.name, RKVersion.uuid from RKAlbum, RKVersion, RKAlbumVersion "
            + "where RKAlbum.modelID = RKAlbumVersion.albumId and "
            + "RKAlbumVersion.versionID = RKVersion.modelId and RKVersion.type = 2 and "
            + "RKVersion.filename not like '%.pdf' and RKVersion.isInTrash = 0"
        )
        for album in c:
            # store by uuid in _dbalbums_uuid and by album in _dbalbums_album
            if not album[1] in self._dbalbums_uuid:
                self._dbalbums_uuid[album[1]] = []
            if not album[0] in self._dbalbums_album:
                self._dbalbums_album[album[0]] = []
            self._dbalbums_uuid[album[1]].append(album[0])
            self._dbalbums_album[album[0]].append(album[1])
            logger.debug("%s %s" % (album[1], album[0]))
            #  set_pbar_status(i)
            i = i + 1
        logger.debug("Finished walking through albums")
        #  close_pbar_status()

        logger.debug("Getting information about keywords")
        c.execute(
            "select count(*) from RKKeyword, RKKeywordForVersion,RKVersion, RKMaster "
            + "where RKKeyword.modelId = RKKeyWordForVersion.keywordID and "
            + "RKVersion.modelID = RKKeywordForVersion.versionID and RKMaster.uuid = "
            + "RKVersion.masterUuid and RKVersion.filename not like '%.pdf' and RKVersion.isInTrash = 0"
        )
        #  init_pbar_status("Keywords", c.fetchone()[0])
        c.execute(
            "select RKKeyword.name, RKVersion.uuid, RKMaster.uuid from "
            + "RKKeyword, RKKeywordForVersion, RKVersion, RKMaster "
            + "where RKKeyword.modelId = RKKeyWordForVersion.keywordID and "
            + "RKVersion.modelID = RKKeywordForVersion.versionID "
            + "and RKMaster.uuid = RKVersion.masterUuid and RKVersion.type = 2 "
            + "and RKVersion.filename not like '%.pdf' and RKVersion.isInTrash = 0"
        )
        i = 0
        for keyword in c:
            if not keyword[1] in self._dbkeywords_uuid:
                self._dbkeywords_uuid[keyword[1]] = []
            if not keyword[0] in self._dbkeywords_keyword:
                self._dbkeywords_keyword[keyword[0]] = []
            self._dbkeywords_uuid[keyword[1]].append(keyword[0])
            self._dbkeywords_keyword[keyword[0]].append(keyword[1])
            logger.debug("%s %s" % (keyword[1], keyword[0]))
            #  set_pbar_status(i)
            i = i + 1
        logger.debug("Finished walking through keywords")
        #  close_pbar_status()

        logger.debug("Getting information about volumes")
        c.execute("select count(*) from RKVolume")
        #  init_pbar_status("Volumes", c.fetchone()[0])
        c.execute("select RKVolume.modelId, RKVolume.name from RKVolume")
        i = 0
        for vol in c:
            self._dbvolumes[vol[0]] = vol[1]
            logger.debug("%s %s" % (vol[0], vol[1]))
            #  set_pbar_status(i)
            i = i + 1
        logger.debug("Finished walking through volumes")
        #  close_pbar_status()

        logger.debug("Getting information about photos")
        c.execute(
            "select count(*) from RKVersion, RKMaster where RKVersion.isInTrash = 0 and "
            + "RKVersion.type = 2 and RKVersion.masterUuid = RKMaster.uuid and "
            + "RKVersion.filename not like '%.pdf'"
        )
        #  init_pbar_status("Photos", c.fetchone()[0])
        c.execute(
            "select RKVersion.uuid, RKVersion.modelId, RKVersion.masterUuid, RKVersion.filename, "
            + "RKVersion.lastmodifieddate, RKVersion.imageDate, RKVersion.mainRating, "
            + "RKVersion.hasAdjustments, RKVersion.hasKeywords, RKVersion.imageTimeZoneOffsetSeconds, "
            + "RKMaster.volumeId, RKMaster.imagePath, RKVersion.extendedDescription, RKVersion.name, "
            + "RKMaster.isMissing "
            + "from RKVersion, RKMaster where RKVersion.isInTrash = 0 and RKVersion.type = 2 and "
            + "RKVersion.masterUuid = RKMaster.uuid and RKVersion.filename not like '%.pdf'"
        )
        i = 0
        for row in c:
            #  set_pbar_status(i)
            i = i + 1
            uuid = row[0]
            if _debug:
                print("i = %d, uuid = '%s, master = '%s" % (i, uuid, row[2]))
            self._dbphotos[uuid] = {}
            self._dbphotos[uuid]["modelID"] = row[1]
            self._dbphotos[uuid]["masterUuid"] = row[2]
            self._dbphotos[uuid]["filename"] = row[3]
            try:
                self._dbphotos[uuid]["lastmodifieddate"] = datetime.fromtimestamp(
                    row[4] + td
                )
            except:
                self._dbphotos[uuid]["lastmodifieddate"] = datetime.fromtimestamp(
                    row[5] + td
                )
            self._dbphotos[uuid]["imageDate"] = datetime.fromtimestamp(row[5] + td)
            self._dbphotos[uuid]["mainRating"] = row[6]
            self._dbphotos[uuid]["hasAdjustments"] = row[7]
            self._dbphotos[uuid]["hasKeywords"] = row[8]
            self._dbphotos[uuid]["imageTimeZoneOffsetSeconds"] = row[9]
            self._dbphotos[uuid]["volumeId"] = row[10]
            self._dbphotos[uuid]["imagePath"] = row[11]
            self._dbphotos[uuid]["extendedDescription"] = row[12]
            self._dbphotos[uuid]["name"] = row[13]
            self._dbphotos[uuid]["isMissing"] = row[14]
            logger.debug(
                "Fetching data for photo %d %s %s %s %s %s: %s"
                % (
                    i,
                    uuid,
                    self._dbphotos[uuid]["masterUuid"],
                    self._dbphotos[uuid]["volumeId"],
                    self._dbphotos[uuid]["filename"],
                    self._dbphotos[uuid]["extendedDescription"],
                    self._dbphotos[uuid]["imageDate"],
                )
            )

        #  close_pbar_status()
        conn.close()

        # add faces and keywords to photo data
        for uuid in self._dbphotos:
            # keywords
            if self._dbphotos[uuid]["hasKeywords"] == 1:
                self._dbphotos[uuid]["keywords"] = self._dbkeywords_uuid[uuid]
            else:
                self._dbphotos[uuid]["keywords"] = []

            if uuid in self._dbfaces_uuid:
                self._dbphotos[uuid]["hasPersons"] = 1
                self._dbphotos[uuid]["persons"] = self._dbfaces_uuid[uuid]
            else:
                self._dbphotos[uuid]["hasPersons"] = 0
                self._dbphotos[uuid]["persons"] = []

            if uuid in self._dbalbums_uuid:
                self._dbphotos[uuid]["albums"] = self._dbalbums_uuid[uuid]
                self._dbphotos[uuid]["hasAlbums"] = 1
            else:
                self._dbphotos[uuid]["albums"] = []
                self._dbphotos[uuid]["hasAlbums"] = 0

            if self._dbphotos[uuid]["volumeId"] is not None:
                self._dbphotos[uuid]["volume"] = self._dbvolumes[
                    self._dbphotos[uuid]["volumeId"]
                ]
            else:
                self._dbphotos[uuid]["volume"] = None

        # remove temporary copy of the database
        try:
            logger.info("Removing temporary database file: " + tmp_db)
            os.remove(tmp_db)
        except:
            print("Could not remove temporary database: " + tmp_db, file=sys.stderr)

        if _debug:
            pp = pprint.PrettyPrinter(indent=4)
            print("Faces:")
            pp.pprint(self._dbfaces_uuid)

            print("Keywords by uuid:")
            pp.pprint(self._dbkeywords_uuid)

            print("Keywords by keyword:")
            pp.pprint(self._dbkeywords_keyword)

            print("Albums by uuid:")
            pp.pprint(self._dbalbums_uuid)

            print("Albums by album:")
            pp.pprint(self._dbalbums_album)

            print("Volumes:")
            pp.pprint(self._dbvolumes)

            print("Photos:")
            pp.pprint(self._dbphotos)

        logger.debug(f"processed {len(self._dbphotos)} photos")

    def photos(self, keywords = [],uuid=[],persons=[],albums=[]):
        photos = []
        if not keywords and not uuid and not persons and not albums:
            #process all the photos
            photos = list(self._dbphotos.keys())
        else:
            if albums is not None:
                for album in albums:
                    print("album=%s" % album)
                    if album in self._dbalbums_album:
                        print("processing album %s:" % album)
                        photos.extend(self._dbalbums_album[album])
                    else:
                        print("Could not find album '%s' in database" %
                            (album), file=sys.stderr)

            if uuid is not None:
                for u in uuid:
                    print("uuid=%s" % u)
                    if u in self._dbphotos:
                        print("processing uuid %s:" % u)
                        photos.extend([u])
                    else:
                        print("Could not find uuid '%s' in database" %
                            (u), file=sys.stderr)

            if keywords is not None:
                for keyword in keywords:
                    print("keyword=%s" % keyword)
                    if keyword in self._dbkeywords_keyword:
                        print("processing keyword %s:" % keyword)
                        photos.extend(self._dbkeywords_keyword[keyword])
                    else:
                        print("Could not find keyword '%s' in database" %
                            (keyword), file=sys.stderr)

            if persons is not None:
                for person in persons:
                    print("person=%s" % person)
                    if person in self._dbfaces_person:
                        print("processing person %s:" % person)
                        photos.extend(self._dbfaces_person[person])
                    else:
                        print("Could not find person '%s' in database" %
                            (person), file=sys.stderr) 

        photoinfo = []
        for p in photos:
            info = PhotoInfo(db = self, uuid = p, info = self._dbphotos[p])
            photoinfo.append(info)
        return photoinfo
    

"""
Info about a specific photo, contains all the details we know about the photo
including keywords, persons, albums, uuid, path, etc.
"""
class PhotoInfo():
    def __init__(self, db = None, uuid = None, info = None):
        self.uuid = uuid
        self.info = info
        self.db = db

    def filename(self):
        return self.info['filename']

    def date(self):
        return self.info['imageDate']    

    """ returns true if photo is missing from disk (which means it's not been downloaded from iCloud) 
        NOTE:   the photos.db database uses an asynchrounous write-ahead log so changes in Photos
                do not immediately get written to disk. In particular, I've noticed that downloading 
                an image from the cloud does not force the database to be updated until something else
                e.g. an edit, keyword, etc. occurs forcing a database synch
                The exact process / timing is a mystery to be but be aware that if some photos were recently
                downloaded from cloud to local storate their status in the database might still show
                isMissing = 1
    """
    def ismissing(self):
        return self.info['isMissing']

    def path(self):
        photopath = ""

        vol = self.info['volume']
        if vol is not None:
            photopath = os.path.join('/Volumes', vol, self.info['imagePath'])
        else:
            photopath = os.path.join(self.db._masters_path, self.info['imagePath'])

        if self.info['isMissing'] == 1:
            logger.warning(f"Skipping photo, not yet downloaded from iCloud: {photopath}")
            print(self.info)
            photopath = None #path would be meaningless until downloaded
            #TODO: Is there a way to use applescript to force the download in this
    
        return photopath 

    def description(self):
        return self.info['extendedDescription']
    
    def persons(self):
        return self.info['persons']
    
    def albums(self):
        return self.info['albums']

    def keywords(self):
        return self.info['keywords']

    def name(self):
        return self.info['name']
    

