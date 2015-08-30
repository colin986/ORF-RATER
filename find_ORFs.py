#! /usr/bin/env python

import argparse
import sys
from Bio import SeqIO
from yeti.genomics.seqtools import seq_to_regex, IUPAC_TABLE_DNA
from yeti.genomics.roitools import Transcript, SegmentChain
import re
from collections import defaultdict
import pandas as pd
import numpy as np
import warnings
import multiprocessing as mp
import subprocess as sp
import os

parser = argparse.ArgumentParser()
parser.add_argument('genomefasta', help='Path to genome FASTA-file')
parser.add_argument('tfamstem', help='Transcript family information generated by make_tfams.py. '
                                     'Both TFAMSTEM.txt and TFAMSTEM.bed should exist or an error will result.')
parser.add_argument('orfstore', help='File to which to output the final table of identified ORFs. Will be formatted as a pandas HDF store (preferred '
                                     'extension is .h5; table name is "all_ORFs"). Different columns of the table indicate various of each ORF, such '
                                     'as start codon, length, etc.')
parser.add_argument('--inbed', type=argparse.FileType('rU'), default=sys.stdin,
                    help='Transcriptome BED-file. Annotated CDSs in this file are assumed to be bona fide CDSs. (Default: stdin)')
parser.add_argument('--codons', nargs='+', default=['ATG'],
                    help='Codons to consider as possible translation initiation sites. All must be 3 nucleotides long. Standard IUPAC nucleotide '
                         'codes are recognized; for example, to query all NTG codons, one could input "NTG" or "ATG CTG GTG TTG" (Default: ATG)')
# parser.add_argument('--extracdsbeds', nargs='+', help='Extra bed files containing additional annotated CDSs beyond (or instead of) those in inbed')
parser.add_argument('--ignoreannotations', action='store_true', help='If flag is set, CDS annotations in INBED will be ignored.')
# ' Typically used in conjunction with --extracdsbeds')
parser.add_argument('-p', '--numproc', type=int, default=1,
                    help='Number of processes to run. Defaults to 1 but recommended to use more (e.g. 12-16)')
opts = parser.parse_args()

for codon in opts.codons:
    if len(codon) != 3 or any(x not in IUPAC_TABLE_DNA for x in codon.upper()):
        raise ValueError('%s is an invalid codon sequence' % codon)
START_RE = seq_to_regex('|'.join(opts.codons), nucleotide_table=IUPAC_TABLE_DNA)
STOP_RE = re.compile(r'(?:...)*?(?:TAG|TAA|TGA)')

# hash transcripts by ID for easy reference later
bedlinedict = {line.split()[3]: line for line in opts.inbed}
if not bedlinedict:
    raise EOFError('Insufficient input or empty file provided')

tfamtids = defaultdict(list)
with open('%s.txt' % opts.tfamstem, 'rU') as tfamtable:
    for line in tfamtable:
        ls = line.strip().split()
        tfamtids[ls[1]].append(ls[0])

with open('%s.bed' % opts.tfamstem, 'rU') as tfambed:
    tfambedlines = {line.split()[3]: line for line in tfambed}

# annot_tfam_lookups = []  # will be ordered like opts.extracdsbeds - will stay empty if no such beds provided
# annot_tid_lookups = []
# if opts.extracdsbeds:
#     import pybedtools  # to handle identifying which tfams get the extra CDSs - otherwise would need to replicate a lot of intersection functionality
#     tfambedtool = pybedtools.BedTool('%s.bed' % opts.tfamstem)
#     for cdsbedfile in opts.extracdsbeds:
#         with open(cdsbedfile, 'rU') as cdsbed:
#             annot_tid_lookups.append({line.split()[3]: line for line in cdsbed})  # as usual, hash bed lines by transcript ID
#         annot_tfam_lookups.append(defaultdict(list))
#         for line in tfambedtool.intersect(pybedtools.BedTool(cdsbedfile), split=True, s=True, wa=True, wb=True):
#             annot_tfam_lookups[-1][line[3]].append(line[15])
#     # after this has finished, each element of annot_tfam_lookup will be a dictionary mapping tfams to lists of transcript IDs in the extra bed files
#     # similarly, each element of annot_tid_lookup will map transcript IDs to BED lines

genome = SeqIO.to_dict(SeqIO.parse(opts.genomefasta, 'fasta'))


def find_all_ORFs(myseq):
    """Identify ORFs, or at least starts.
    Returns list of (start,stop,codon), where stop == 0 if no valid stop codon is present.
    Starts are NTGs
    """
    result = []
    for i in range(len(myseq)-2):
        if START_RE.match(myseq[i:i+3]):
            m = STOP_RE.match(myseq[i:])
            if m:
                result.append((i, m.end()+i, myseq[i:i+3]))
            else:
                result.append((i, 0, myseq[i:i+3]))
    return result


def name_ORF(tfam, gcoord, AAlen):
    return '%s_%d_%daa' % (tfam, gcoord, AAlen)


def identify_tfam_ORFs((tfam, tids)):
    currtfam = SegmentChain.from_bed(tfambedlines[tfam])
    chrom = currtfam.chrom
    strand = currtfam.strand
    tfam_gcoords = np.array(currtfam.get_position_list(stranded=True))
    tfam_dfs = []
    tmask = np.empty((len(tids), len(tfam_gcoords)), dtype=np.bool)  # True if transcript covers that position, False if not
    tidx_lookup = {}
    for tidx, tid in enumerate(tids):
        tidx_lookup[tid] = tidx
        curr_trans = Transcript.from_bed(bedlinedict[tid])
        tmask[tidx, :] = np.in1d(tfam_gcoords, curr_trans.get_position_list(stranded=True), assume_unique=True)
        trans_ORF = find_all_ORFs(curr_trans.get_sequence(genome).upper())
        if trans_ORF:
            (startpos, stoppos, codons) = zip(*trans_ORF)
            startpos = np.array(startpos)
            stoppos = np.array(stoppos)

            if opts.ignoreannotations or curr_trans.cds_start is None:
                annot_cds = np.zeros(len(trans_ORF), dtype=np.bool)
            else:
                annot_cds = startpos == curr_trans.cds_start
                assert (annot_cds.sum() <= 1)  # this would only happen if find_all_ORFs() isn't working correctly
                if annot_cds.sum() == 1:
                    annot_idx = np.flatnonzero(annot_cds)[0]
                    if stoppos[annot_idx] != curr_trans.cds_end:  # e.g. truncated transcript, frameshift, selenoprotein
                        warnings.warn('Annotated CDS in transcript %s (tfam %s) appears malformed; ignoring' % (tid, tfam))
                        annot_cds[annot_idx] = False  # un-annotate it; others are already False

            gcoords = np.array(curr_trans.get_genomic_coordinate(startpos)[1], dtype='u4')

            stop_present = (stoppos > 0)
            gstops = np.zeros(len(trans_ORF), dtype='u4')
            gstops[stop_present] = curr_trans.get_genomic_coordinate(stoppos[stop_present] - 1)[1] + (strand == '+')*2 - 1
            # the decrementing/incrementing stuff preserves half-openness regardless of strand

            AAlens = np.zeros(len(trans_ORF), dtype='u4')
            AAlens[stop_present] = (stoppos[stop_present] - startpos[stop_present])/3 - 1
            tfam_dfs.append(pd.DataFrame.from_items([('tfam', tfam),
                                                     ('tid', tid),
                                                     ('tcoord', startpos),
                                                     ('tstop', stoppos),
                                                     ('chrom', chrom),
                                                     ('gcoord', gcoords),
                                                     ('gstop', gstops),
                                                     ('strand', strand),
                                                     ('codon', codons),
                                                     ('AAlen', AAlens),
                                                     ('annot', annot_cds),
                                                     ('ORF_name', '')]))
    if any(x is not None for x in tfam_dfs):
        tfam_dfs = pd.concat(tfam_dfs, ignore_index=True)
        # curr_named_ORFs = []
        for ((gcoord, AAlen), gcoord_grp) in tfam_dfs.groupby(['gcoord', 'AAlen']):  # group by genomic start position and length
            if len(gcoord_grp) == 1:
                tfam_dfs.loc[gcoord_grp.index, 'ORF_name'] = name_ORF(tfam, gcoord, AAlen)
            else:
                ORF_gcoords = np.vstack(np.flatnonzero(tmask[tidx_lookup[tid], :])[tcoord:tstop]
                                        for (tid, tcoord, tstop) in gcoord_grp[['tid', 'tcoord', 'tstop']].itertuples(False))
                if (ORF_gcoords == ORF_gcoords[0, :]).all():  # all of the grouped ORFs are identical, so should receive the same name
                    tfam_dfs.loc[gcoord_grp.index, 'ORF_name'] = name_ORF(tfam, gcoord, AAlen)
                    # gcoord_grp['ORF_name'] = name_ORF(tfam, gcoord, AAlen)
                    # gcoord_grp['annot'] = gcoord_grp['annot'].any()  # if one is annotated, all are annotated
                else:
                    named_so_far = 0
                    # gcoord_grp['ORF_name'] = ''
                    unnamed = np.ones(len(gcoord_grp), dtype=np.bool)
                    basename = name_ORF(tfam, gcoord, AAlen)
                    while unnamed.any():
                        identicals = (ORF_gcoords == ORF_gcoords[unnamed, :][0, :]).all(1)
                        tfam_dfs.loc[gcoord_grp.index[identicals], 'ORF_name'] = '%s_%d' % (basename, named_so_far)
                        # gcoord_grp.loc[identicals, 'ORF_name'] = '%s_%d' % (basename, named_so_far)
                        # # gcoord_grp.loc[identicals, 'annot'] = gcoord_grp.loc[identicals, 'annot'].any()
                        unnamed[identicals] = False
                        named_so_far += 1
            # curr_named_ORFs.append(gcoord_grp)
        return tfam_dfs
        # nonredundant_ORFs = curr_named_ORFs.drop_duplicates('ORF_name')
        # for (annot_tfam_lookup, annot_tid_lookup) in zip(annot_tfam_lookups, annot_tid_lookups):
        #     for tid in annot_tfam_lookup[tfam]:
        #         curr_trans = Transcript.from_bed(annot_tid_lookup[tid])
        #         curr_gcoord = curr_trans.get_genomic_coordinate(curr_trans.cds_start)[1]
        #         curr_gstop = curr_trans.get_genomic_coordinate(curr_trans.cds_end-1)[1]+(strand == '+')*2-1
        #         shared_start = (nonredundant_ORFs['gcoord'] == curr_gcoord)
        #         shared_stop = (nonredundant_ORFs['gstop'] == curr_gstop)
        #         curr_cds_pos = curr_trans.get_cds_IVC().get_position_set()
        #         shared_len = (nonredundant_ORFs['tstop']-nonredundant_ORFs['tcoord'] == len(curr_cds_pos))
        #         if (shared_start & shared_stop & shared_len).any():
    else:
        return None


workers = mp.Pool(opts.numproc)
all_ORFs = pd.concat(workers.map(identify_tfam_ORFs, tfamtids.iteritems()), ignore_index=True)
workers.close()

for catfield in ['chrom', 'strand', 'codon']:
    all_ORFs[catfield] = all_ORFs[catfield].astype('category')  # saves disk space and read/write time

all_ORFs.to_hdf(opts.orfstore+'.tmp', 'all_ORFs', format='t', data_columns=True, complevel=1, complib='blosc')
sp.call(['ptrepack', opts.orfstore+'.tmp', opts.orfstore])  # repack for efficiency
os.remove(opts.orfstore+'.tmp')

# could write as tab-delimited text, but read performance on this table is pretty important