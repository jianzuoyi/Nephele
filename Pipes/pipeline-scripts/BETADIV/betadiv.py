#!/usr/bin/python3
import os
import json
import csv
import cfg as neph_cfg
from collections import namedtuple
from itertools import groupby
import logging
from sh import mkdir, wget, unzip, guess_fq_score_type
from optparse import OptionParser
import subprocess
import multiprocessing
import neph_errors
import tarfile

# get body site
# learn relevant sample IDs (#SampleID) from PSN file, write to a file.
# write every line of body site of interest (HMPbodysubsite) to a file.
# run filter_samples_from_otu_table.py using the above.
# done.
#     Steps for comparison (see logfile.txt)
# a) Merge biom files of user with with biom file for HMP (otu_table_psn_v13.biom) using qiime Script "merge_otu_tables.py"
# b) Merge mapping file of user with mapping file of HMP (v13_map_uniquebyPSN.txt) using qiime script "merge_mapping_files.py"
# c) Run beta diversity without a tree file and using bray_curtis using qiime script
# beta_diversity.py -i merged.biom -m bray_curtis -o merged_dir
# d) sort distance matrix using Alex custom script and parameters to select the closest matches (a defined number) and report the IDs of the HMP matches using the qiime script
# f) filter biom file to retain only those IDs for the closest matches using qiime script (see line: filter_samples_from_otu_table.py -i ../merged.biom -o selected.biom --sample_id_fp ../S_023556.list -m ../merged_map.txt --output_mapping_fp selected.map_txt)
# g) run script in qiime to plot bargraphs

IDs_to_dist = namedtuple('IDs_to_dist', ['user_s_id','DACC_s_id','distance'] )
Sample = namedtuple('Sample', ['sample_id',
                               'BarcodeSequence',
                               'ForwardFastqFile',
                               'ReverseFastqFile',
                               'Head',
                               'Line'] )

class Cfg:
    _FILE_ROOT = '/home/ubuntu/ref_dbs/otus/beta_div_analysis_files/'
    _BASE = 'HMP_compare_results/'

    DAC_body_sites = ['Anterior_nares',
                      'Attached_Keratinized_gingiva',
                      'Buccal_mucosa',
                      'Hard_palate',
                      'HMPbodysubsite',
                      'Left_Antecubital_fossa',
                      'Left_Retroauricular_crease',
                      'Mid_vagina',
                      'Palatine_Tonsils',
                      'Posterior_fornix',
                      'Right_Antecubital_fossa',
                      'Right_Retroauricular_crease',
                      'Saliva',
                      'Stool',
                      'Subgingival_plaque',
                      'Supragingival_plaque',
                      'Throat',
                      'Tongue_dorsum',
                      'Vaginal_introitus']
    composite_sites = ['Oral_cavity', 'Skin', 'Urogenital_tract']

    HMP_SPLT_OUT_DIR = _BASE + 'hmp_split_lib_outs'
    USER_SPLT_OUT_DIR = 'split_lib_out'

    HMP_CLOSED_OTUS_OUT_DIR = _BASE + 'hmp_closed_otus_outs'
    USER_CLOSED_OTUS_OUT_DIR = _BASE + 'user_closed_otus_outs'
    DACC_BIOM_FILE = HMP_CLOSED_OTUS_OUT_DIR + '/otu_table.biom'
    USER_BIOM_FILE = USER_CLOSED_OTUS_OUT_DIR + '/otu_table.biom'

    MERGED_BIOM_FILE_OUT = _BASE + 'user_hmp_merged.biom'
    MERGED_MAP_FILE_OUT = _BASE + 'user_hmp_merged.map'
    BETA_PLOTS_OUT_DIR = _BASE + 'beta_diversity_plots'
    TAX_LEVEL_PLOTTED = 4
    SUMMARIZE_TAXA_OUT_DIR = _BASE + 'per_sample_biom_files/'

    BETA_DIV_OUT_DIR = _BASE + 'beta_diversity_outs'
    BETA_DIV_OUT_FNAME = BETA_DIV_OUT_DIR + '/bray_curtis_user_hmp_merged.txt'
    HMP_DB_TAR = 'HMP_ref_dbs.tar.gz'
    HMP_PARAMS = 'HMP_params.txt'


def setup_logger( log_name ):
    formatter = logging.Formatter(fmt='[ %(asctime)s - %(levelname)s ] %(message)s\n')
    if not os.path.isdir(Cfg._BASE):
        mkdir(Cfg._BASE)
    fh = logging.FileHandler( Cfg._BASE + 'logfile_cmp_to_HMP.txt' )
    fh.setLevel(logging.INFO)
    fh.setFormatter(formatter)
    logger = logging.getLogger(log_name)
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    return logger

def exec_cmnd( cmds, log ):
    if cmds is None:
        return
    if isinstance(cmds, str):
        l = list()
        l.append( cmds )
        cmds = l
    while len(cmds) > 0:
        cmd = cmds.pop()
        log.info( cmd )
        try:
            if cmd.startswith('mothur'): # this might not be needed
                os.system(cmd)
            else:
                e = subprocess.check_output(cmd.split(), stderr=subprocess.STDOUT)
                if len(e) > 0:
                    print(e)
        except subprocess.CalledProcessError as cpe:
            out_bytes = cpe.output       # Output generated before error

def gen_filter_cmnd( input_biom, sample_ids_file, mapping_fp, output_mapping_fp, output_biom ):
    return 'filter_samples_from_otu_table.py '\
        ' --input_fp=' + input_biom\
        + ' --sample_id_fp='+ sample_ids_file \
        + ' --mapping_fp=' + mapping_fp\
        + ' --output_mapping_fp='+ output_mapping_fp\
        + ' --output_fp=' + output_biom

def gen_summarize_taxa_cmd( biom_file, sample ):
    return 'summarize_taxa.py '\
        + ' --otu_table_fp=' + biom_file\
        + ' --output_dir=' + Cfg.SUMMARIZE_TAXA_OUT_DIR + sample

def gen_hmp_params( params ):
    line = "pick_otus:otu_picking_method usearch61_ref\npick_otus:enable_rev_strand_match True\n"
    with open(params, 'w') as par:
        par.write( line )
        par.close()

def gen_merge_otu_tables_cmd(biom_file_a, biom_file_b):
    return 'merge_otu_tables.py '\
        + ' --input_fps=' + biom_file_a + ',' + biom_file_b\
        + ' --output_fp=' + Cfg.MERGED_BIOM_FILE_OUT

def gen_merge_map_files_cmd(map_file_a, map_file_b):
    return 'merge_mapping_files.py '\
        + ' --mapping_fps=' + ','.join([map_file_a, map_file_b])\
        + ' --no_data_value=NO_DATA'\
        + ' --output_fp=' + Cfg.MERGED_MAP_FILE_OUT

def gen_beta_diversity_cmd( biom_file ):
    return 'beta_diversity.py '\
        + ' --input_path=' + biom_file \
        + ' --metrics=bray_curtis'\
        + ' --output_dir=' + Cfg.BETA_DIV_OUT_DIR

def gen_beta_diversity_through_plots( biom_file, map_file, tree_file, out_dir ):
    return 'beta_diversity_through_plots.py '\
        + ' --otu_table_fp=' + biom_file \
        + ' --mapping_fp='+ map_file\
        + ' --tree_fp='+ tree_file\
        + ' --parameter_fp=betadiv_params.txt'\
        + ' --output_dir=' + out_dir

def gen_phyloseq_images_cmd( biom_file, map_file, taxa ):
    return 'Rscript betterplots.R '\
                       + " " + biom_file\
                       + " " + map_file\
                       + " " + taxa\
                       + " YES"

def gen_plot_taxa_summary_cmd( counts_fname, out_dir ):
    return 'plot_taxa_summary.py '\
        + ' --counts_fname=' + counts_fname\
        + ' --dir_path=' + Cfg.SUMMARIZE_TAXA_OUT_DIR + out_dir

# this seems to hang
# def gen_make_otu_heatmap_cmd ( biom_file ):
#     return 'make_otu_heatmap.py '\
#         ' --otu_table_fp=' + biom_file\
#         + ' --imagetype=png'\
#         + ' --output_fp=' + Cfg.OTU_HEATMAP_OUT_DIR

def find_max_float_in_file( fname ):
    with open(fname, 'r') as f_in:
        reader = csv.reader(f_in, delimiter='\t')
        next(reader) # ignore the first line
        max_vals = list( max(row[1:-1]) for row in reader )
        return float(max(max_vals))

def gen_sample_sample_dist_dict( user_samples, dist_matrix ):
    distances = list()
    samples = [ s.sample_id for s in user_samples ]
    with open(dist_matrix, 'r') as f_in:
        lines = f_in.read().strip().splitlines()
        sample_ids_top_line = lines.pop(0).split("\t")
        for line in lines:
            elts = line.split("\t")
            sample_row_id = elts.pop(0)
            if sample_row_id not in samples:
                continue
            for index, dist in enumerate(elts):
                sample_y = sample_ids_top_line[index]
                if sample_y == sample_row_id or sample_y in samples:
                    continue
                i = IDs_to_dist(user_s_id = sample_row_id, DACC_s_id = sample_y, distance = float(dist))
                distances.append(i)
    return distances

def print_n_samples_to_file( samples, n ):
    if n > len(samples):
        n = len(samples)
    user_s_id = samples[0].user_s_id
    if not os.path.isdir( user_s_id ):
        mkdir( user_s_id )
    fname = user_s_id + '/' + user_s_id + '.list'
    with open(fname, 'w') as f_out:
        print ("\t".join(['S_ID', 'Distance']), file=f_out)
        print ("\t".join([user_s_id, '0.0']), file=f_out)
        for i in range(0, n):
            print ("\t".join([ samples[i].DACC_s_id, str( samples[i].distance )]), file=f_out)
    return fname

def get_DACC_region_file( region ):
    fname = region + '_reads.zip'
    if not os.path.isfile(fname):
        wget('' + fname)
        unzip('-o', fname)

def get_DACC_BIOM_file( database, body_site, region_dacc ):
    log = setup_logger('COMPARE_TO_HMP')
    if database == "Greengenes_99":
        database = "biom-gg-99"
    elif database == "Greengenes_97":
        database = "biom-gg-97"
    elif database == "SILVA_99":
        database = "biom-silva-99"
    elif database == "SILVA_97":
        database = "biom-silva-97"
    else:
        log.error(neph_errors.NO_HMP_DATABASE)

    fname = '' + database + '/' + body_site + '_' + region_dacc + '.mapping.biom'
    if not os.path.isfile(os.path.basename(fname)):
        wget(fname)
    return os.path.basename(fname)

def get_DACC_map_file (body_site, region_dacc):
    fname = '' + body_site + '_' + region_dacc +'.mapping.txt'
    if not os.path.isfile(os.path.basename(fname)):
        wget(fname)
    return os.path.basename(fname)

def get_reference_DBs ( dbs ):
    fname = '' + dbs
    if not os.path.isfile(os.path.basename(fname)) & os.path.exists( 'HMP_ref_dbs' ) == False:
        wget(fname)
        archive = tarfile.open(dbs, 'r:gz')
        archive.extractall('.')
    return os.path.basename(fname)


def gen_closed_reference_cmd( inputs, out_dir, ref_fasta_file, ref_taxonomy_file, params ):
    #        + ' --parallel'\ < this blows things up
    # + ' --jobs_to_start='+ str(int(multiprocessing.cpu_count() / 4))\
    return  "pick_closed_reference_otus.py "\
        + " -i " + inputs\
        + " --output_dir=" + out_dir \
        + ' --reference_fp=' + ref_fasta_file \
        + ' --taxonomy_fp=' + ref_taxonomy_file \
        + ' -p ' + params \
        + ' --force'

def gen_split_lib_for_fastq_cmd( samples, dacc_map_file, out_dir ):
    inputs = list()          # seq file names
    sample_ids = list()      # all sample IDs
    for sample in samples:
        inputs.append(sample.ForwardFastqFile)
        sample_ids.append(sample.sample_id)
    phred_offset = guess_fq_score_type(samples[0].ForwardFastqFile)
    s = 'split_libraries_fastq.py '\
        + ' --output_dir=' + out_dir\
        + ' --barcode_type=not-barcoded'\
        + ' --mapping_fps=' + dacc_map_file \
        + ' --phred_offset=' + str(phred_offset)\
        + ' -i ' + ','.join(inputs)\
        + ' --sample_ids=' + ','.join(sample_ids)
    return s

def gen_samples( fname ):
    # Mothur Paired End map
    #SampleID BarcodeSequence LinkerPrimerSequence	ForwardFastqFile ReverseFastqFile TreatmentGroup	ReversePrimer	Description

    # Mothur 454 map
    #SampleID BarcodeSequence LinkerPrimerSequence                                    TreatmentGroup  Description

    # qiime
    #SampleID BarcodeSequence LinkerPrimerSequence	TreatmentGroup	ExtraTestCol	More	Description
    with open(fname) as f:
        reader = csv.DictReader( f, delimiter='\t' )
        samples = list()
        for row in reader:
            if 'ForwardFastqFile' in row.keys() and 'ReverseFastqFile' in row.keys():
                sample = Sample( sample_id = row['#SampleID'],
                                 BarcodeSequence = row['BarcodeSequence'],
                                 ForwardFastqFile = row['ForwardFastqFile'],
                                 ReverseFastqFile = row['ReverseFastqFile'],
                                 Head = row.keys(),
                                 Line = row.values() )
            elif 'ForwardFastqFile' in row.keys():
                sample = Sample( sample_id = row['#SampleID'],
                                 BarcodeSequence = row['BarcodeSequence'],
                                 ForwardFastqFile = row['ForwardFastqFile'],
                                 ReverseFastqFile = '',
                                 Head = row.keys(),
                                 Line = row.values() )
            else:
                sample = Sample( sample_id = row['#SampleID'],
                                 BarcodeSequence = row['BarcodeSequence'],
                                 ForwardFastqFile = '',
                                 ReverseFastqFile = '',
                                 Head = row.keys(),
                                 Line = row.values() )
            samples.append(sample)

        # samples = [ Sample( sample_id = row['#SampleID'], ForwardFastqFile = row['ForwardFastqFile'] )
        #             if 'ForwardFastqFile' in row
        #             else Sample( sample_id = row['#SampleID'], ForwardFastqFile = '')
        #             for row in reader ]
        return samples

def gen_split_libraries_cmd( samples, map_file, seqs_file, qual_file ):
    return 'split_libraries.py '\
        + ' --dir_prefix=split_lib_out'\
        + ' --barcode_type=' + str(len(samples[0].BarcodeSequence))\
        + ' --min_seq_length=200'\
        + ' --max_ambig=6'\
        + ' --max_homopolymer=6'\
        + ' --max_primer_mismatch=0'\
        + ' --max_barcode_errors=1.5'\
        + ' --disable_bc_correction'\
        + ' --qual_score_window=50'\
        + ' --max_seq_length=1000'\
        + ' --min_qual_score=25'\
        + ' --map=' + map_file\
        + ' --fasta=' + seqs_file\
        + ' --qual=' + qual_file


def gen_split_libraries_mothur_PE( samples, maps ):
    # + cfg.SPLIT_LIB_OUT_DIR \
    inputs = list()
    maps = list()
    sample_ids = list()
    phred_offset = guess_fq_score_type(samples[0].ForwardFastqFile)

    for sample in samples:
        inputs.append(sample.sample_id + '/fastqjoin.join.fastq')
        maps.append(sample.sample_id + '/map.txt')
        sample_ids.append(sample.sample_id)

    return 'split_libraries_fastq.py '\
        + ' --output_dir=split_lib_out'\
        + ' --phred_quality_threshold=25'\
        + ' --sequence_max_n=0'\
        + ' --barcode_type=not-barcoded ' \
        + ' --max_bad_run_length=3'\
        + ' --sequence_read_fps=' + ','.join(inputs) \
        + ' --mapping_fps=' + ','.join(maps) \
        + ' --phred_offset=' + str(phred_offset)\
        + ' --sample_ids=' + ','.join(sample_ids)

def gen_per_sample_single_map_file ( samples ):
    map_files = list()
    for sample in samples:
        dname = sample.sample_id
        if not os.path.isdir( dname ):
            log = setup_logger('COMPARE_TO_HMP')
            log.error('No output directory:{0}'.format(dname))
        else:
            # head = ['#SampleID','BarcodeSequence','LinkerPrimerSequence','ForwardFastqFile',\
            #         'ReverseFastqFile','TreatmentGroup','Description']
            # line = [sample.sample_id, sample.BarcodeSequence, sample.LinkerPrimerSequence, \
            #         sample.ForwardFastqFile, sample.ReverseFastqFile, \
            #         sample.TreatmentGroup, sample.Description]
            with open ( dname + '/' + 'map.txt', 'w' ) as f_out:
                print ("\t".join( sample.Head ), file = f_out)
                print ("\t".join( sample.Line ), file = f_out)
            map_files.append(f_out)
    return f_out

def gen_join_paired_end_cmd( samples ):
    cmds = list()
    for sample in samples:
        cmd = 'join_paired_ends.py '\
            + ' --output_dir=' + sample.sample_id\
            + ' --forward_reads_fp=' + sample.ForwardFastqFile\
            + ' --reverse_reads_fp=' + sample.ReverseFastqFile\
            + ' --perc_max_diff=25'\
            + ' --min_overlap=10'
        cmds.append(cmd)
    return cmds

def main( inputs ):
    log = setup_logger('COMPARE_TO_HMP')
    DACC_map_file = get_DACC_map_file( inputs.body_site, inputs.region_dacc )
    #    DACC_reads_fname = get_DACC_region_file( inputs.region_dacc )
    DACC_BIOM_FILE = get_DACC_BIOM_file( inputs.hmp_database, inputs.body_site, inputs.region_dacc )
    DACC_samples = gen_samples( DACC_map_file )
    user_samples = gen_samples( inputs.map_file )
    HMP_DB = get_reference_DBs( Cfg.HMP_DB_TAR )
    exec_cmnd( gen_hmp_params( Cfg.HMP_PARAMS ), log)

#    exec_cmnd( gen_split_lib_for_fastq_cmd( DACC_samples, DACC_map_file, Cfg.HMP_SPLT_OUT_DIR), log )
    if inputs.user_seqs == 'rawfile.fasta': # this implies we're running Mothur 454
        exec_cmnd( gen_split_libraries_cmd( user_samples,
                                            inputs.map_file,
                                            'rawfile.fasta',
                                            'rawfile.qual'), log )
    elif inputs.user_seqs == 'fileList.paired.file': # this implies Mothur MiSeq
        exec_cmnd( gen_join_paired_end_cmd( user_samples ), log )
        # conf.ensure_output_exists( conf.get_join_paired_end_outputs() )
        maps = gen_per_sample_single_map_file( user_samples )
        exec_cmnd( gen_split_libraries_mothur_PE( user_samples, maps ), log)

    # else we already have split_lib_out/seqs.fna

    # for DACC
#    exec_cmnd(gen_closed_reference_cmd( Cfg.HMP_SPLT_OUT_DIR + '/seqs.fna',
#                                        Cfg.HMP_CLOSED_OTUS_OUT_DIR,
#                                        neph_cfg.HMP_REF_FASTA_FILE[inputs.hmp_database],
#                                        neph_cfg.HMP_REF_TAXONOMY_FILE[inputs.hmp_database] ),log)
    # for user
    exec_cmnd(gen_closed_reference_cmd( Cfg.USER_SPLT_OUT_DIR + '/seqs.fna',
                                        Cfg.USER_CLOSED_OTUS_OUT_DIR,
                                        neph_cfg.HMP_REF_FASTA_FILE[inputs.hmp_database],
                                        neph_cfg.HMP_REF_TAXONOMY_FILE[inputs.hmp_database],
                                        Cfg.HMP_PARAMS ),log)

    exec_cmnd(gen_merge_otu_tables_cmd( DACC_BIOM_FILE, Cfg.USER_BIOM_FILE ), log)
    exec_cmnd(gen_merge_map_files_cmd( DACC_map_file, inputs.map_file ), log)
    exec_cmnd(gen_beta_diversity_cmd( Cfg.MERGED_BIOM_FILE_OUT ), log )
    exec_cmnd(gen_beta_diversity_through_plots( Cfg.MERGED_BIOM_FILE_OUT,
                                                Cfg.MERGED_MAP_FILE_OUT,
                                                neph_cfg.HMP_TREE_FILE[inputs.hmp_database],
                                                Cfg.BETA_PLOTS_OUT_DIR ), log)
    s_ids_to_dist = gen_sample_sample_dist_dict( user_samples, Cfg.BETA_DIV_OUT_FNAME )
    max_dist = find_max_float_in_file( Cfg.BETA_DIV_OUT_FNAME )
    by_dist = sorted(s_ids_to_dist, key=lambda i: (i.user_s_id, i.distance))
    normalized = list( IDs_to_dist( user_s_id = sample.user_s_id,
                                    DACC_s_id = sample.DACC_s_id,
                                    distance = sample.distance / max_dist) for sample in by_dist )

    for sample, items in groupby( normalized, key=lambda i: i.user_s_id ):
        group = list(items)
        if group[0].distance == 1:
            log.warning('Sample {0} is unrelated to DACC. Unable to perform further analysis.'
                        .format(sample))
        else:
            id_file = print_n_samples_to_file( group, int(inputs.nearest_n_samples) )
            exec_cmnd( gen_filter_cmnd( Cfg.MERGED_BIOM_FILE_OUT,
                                        id_file,
                                        Cfg.MERGED_MAP_FILE_OUT,
                                        id_file + '.map',
                                        id_file + '.biom'), log )
            exec_cmnd( gen_summarize_taxa_cmd(id_file + '.biom', sample), log)
            in_fname_base = id_file + '_L' + str(Cfg.TAX_LEVEL_PLOTTED)
#            exec_cmnd( gen_plot_taxa_summary_cmd('HMP_compare_results/taxa_plots/' + in_fname_base + '.txt', sample), log)
            taxa_levels = ["Phylum", "Class", "Order", "Family", "Genus"]
            for taxa in taxa_levels:
                exec_cmnd( gen_phyloseq_images_cmd( id_file + '.biom', id_file + '.map', taxa), log)
            #           exec_cmnd( gen_make_otu_heatmap_cmd( id_file + '.biom' ), log)

if __name__ == "__main__":
    parser = OptionParser()
    parser.add_option("--body_site",
                      type = "choice",
                      choices = Cfg.DAC_body_sites + Cfg.composite_sites,
                      help="Select one of:" + ','.join(Cfg.composite_sites + Cfg.DAC_body_sites) )
    parser.add_option( "--map_file", type = "string" )
    parser.add_option( "-n", "--nearest_n_samples", type = "int") # MAX_NUM
    parser.add_option( "--hmp_database", type = "choice", choices = ["Greengenes_97",
                                                                     "Greengenes_99",
                                                                     'SILVA_97', "SILVA_99"] )
    parser.add_option( "--region_dacc", type = "choice", choices = [ "v1v3", "v3v5", "v6v9" ] )
    parser.add_option( "--user_seqs", type = "string", default="qiime" )
    (options, args) = parser.parse_args()
    if not os.path.isfile(options.user_seqs):
        print("Ensure sequence file exists, cannot find:" + options.user_seqs)
        exit(1)

    if not os.path.isfile(options.map_file):
        print("Ensure your map file exists, cannot find:" + options.map_file)
        exit(1)
    main(options)
