#!/bin/sh

CALIBRE_DIR=/Applications/calibre.app/Contents/console.app/Contents/MacOS

$CALIBRE_DIR/calibre-customize -b .
$CALIBRE_DIR/calibre-debug -e __init__.py
