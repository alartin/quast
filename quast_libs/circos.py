############################################################################
# Copyright (c) 2015-2017 Saint Petersburg State University
# Copyright (c) 2011-2015 Saint Petersburg Academic University
# All Rights Reserved
# See file LICENSE for details.
############################################################################

from __future__ import with_statement

import os
import shutil
from collections import defaultdict
from os.path import join, exists, dirname, realpath

try:
   from collections import OrderedDict
except ImportError:
   from quast_libs.site_packages.ordered_dict import OrderedDict

from quast_libs import qutils, qconfig
from quast_libs.ca_utils.align_contigs import get_nucmer_aux_out_fpaths
from quast_libs.ca_utils.misc import create_nucmer_output_dir, open_gzipsafe, ref_labels_by_chromosomes
from quast_libs.fastaparser import get_chr_lengths_from_fastafile
from quast_libs.icarus_utils import get_assemblies, check_misassembled_blocks, Alignment
from quast_libs.qutils import get_path_to_program, is_non_empty_file, relpath
from quast_libs.reads_analyzer import COVERAGE_FACTOR

circos_png_fname = 'circos.png'
TRACK_WIDTH = 0.04
TRACK_INTERVAL = 0.04
BIG_TRACK_INTERVAL = 0.06
MAX_POINTS = 50000


def create_ideogram(chr_lengths, output_dir):
    num_chromosomes = 0
    max_len = 0
    karyotype_fpath = join(output_dir, 'reference.karyotype.txt')
    with open(karyotype_fpath, 'w') as out_f:
        for name, seq_len in chr_lengths.items():
            out_f.write('\t'.join(['chr', '-', name, name, '0', str(seq_len), 'lgrey']) + '\n')
            max_len = max(max_len, seq_len)
            num_chromosomes += 1

    ideogram_fpath = join(output_dir, 'ideogram.conf')
    with open(ideogram_fpath, 'w') as out_f:
        out_f.write('<ideogram>\n')
        out_f.write('<spacing>\n')
        if qconfig.prokaryote and num_chromosomes == 1:
            out_f.write('default = 0r\n')  # circular chromosome
        elif num_chromosomes <= 30:
            out_f.write('default = 0.005r\n')
        elif num_chromosomes <= 100:
            out_f.write('default = 0.001r\n')
        else:
            out_f.write('default = 0.0005r\n')
        out_f.write('break = 0.005r\n')
        out_f.write('</spacing>\n')
        out_f.write('thickness = 30p\n')
        out_f.write('stroke_thickness = 2\n')
        out_f.write('stroke_color = black\n')
        out_f.write('fill = yes\n')
        out_f.write('radius = 0.85r\n')
        out_f.write('show_label = no\n')
        out_f.write('label_font = default\n')
        out_f.write('label_radius = dims(ideogram,radius) + 0.05r\n')
        out_f.write('label_size = 36\n')
        out_f.write('label_parallel = yes\n')
        out_f.write('band_stroke_thickness = 2\n')
        out_f.write('show_bands = yes\n')
        out_f.write('fill_bands = yes\n')
        out_f.write('</ideogram>')
    return max_len, karyotype_fpath, ideogram_fpath


def create_ticks_conf(chrom_units, output_dir):
    ticks_fpath = join(output_dir, 'ticks.conf')
    with open(ticks_fpath, 'w') as out_f:
        out_f.write('show_ticks = yes\n')
        out_f.write('show_tick_labels = yes\n')
        out_f.write('show_grid = no\n')
        out_f.write('<ticks>\n')
        out_f.write('skip_first_label = yes\n')
        out_f.write('skip_last_label = no\n')
        out_f.write('radius = dims(ideogram,radius_outer)\n')
        out_f.write('tick_separation = 2p\n')
        out_f.write('min_label_distance_to_edge = 0p\n')
        out_f.write('label_separation = 5p\n')
        out_f.write('label_offset = 5p\n')
        out_f.write('label_size = 12p\n')
        out_f.write('thickness = 3p\n')
        if chrom_units * 10 >= 10 ** 6:
            label_multiplier = 1.0 / (chrom_units * 10)
            suffix = 'Mbp'
        else:
            label_multiplier = 1.0 / chrom_units
            suffix = 'kbp'
        out_f.write('label_multiplier = ' + str(label_multiplier) + '\n')
        out_f.write('<tick>\n')
        out_f.write('spacing = 1u\n')
        out_f.write('color = dgrey\n')
        out_f.write('size = 12p\n')
        out_f.write('show_label = no\n')
        out_f.write('format = %s\n')
        out_f.write('</tick>\n')
        out_f.write('<tick>\n')
        out_f.write('spacing = 5u\n')
        out_f.write('color = black\n')
        out_f.write('size = 18p\n')
        out_f.write('show_label = yes\n')
        out_f.write('label_size = 24p\n')
        out_f.write('format = %s\n')
        out_f.write('</tick>\n')
        out_f.write('<tick>\n')
        out_f.write('spacing = 10u\n')
        out_f.write('color = black\n')
        out_f.write('size = 24p\n')
        out_f.write('show_label = yes\n')
        out_f.write('label_size = 32p\n')
        out_f.write('suffix = " %s"\n' % suffix)
        out_f.write('format = %s\n')
        out_f.write('</tick>\n')
        out_f.write('</ticks>')
    return ticks_fpath


def create_meta_highlights(chr_lengths, output_dir):
    highlights_fpath = join(output_dir, 'highlights.txt')
    colors = ['orange', 'purple', 'blue']
    with open(highlights_fpath, 'w') as out_f:
        chrom_by_refs = OrderedDict()
        for chrom, ref in ref_labels_by_chromosomes.items():
            if not ref in chrom_by_refs:
                chrom_by_refs[ref] = []
            chrom_by_refs[ref].append(chrom)
        for i, (ref, chromosomes) in enumerate(chrom_by_refs.items()):
            for chrom in chromosomes:
                out_f.write('\t'.join([chrom, '0', str(chr_lengths[chrom]), 'fill_color=' + colors[i % len(colors)]]) + '\n')
    return highlights_fpath

def parse_nucmer_contig_report(report_fpath):
    aligned_blocks = []
    misassembled_id_to_structure = defaultdict(list)

    with open(report_fpath) as report_file:
        contig_id = None

        start_col = None
        end_col = None
        ref_col = None
        contig_col = None
        ambig_col = None
        best_col = None
        for i, line in enumerate(report_file):
            split_line = line.replace('\n', '').split('\t')
            if i == 0:
                start_col = split_line.index('S1')
                end_col = split_line.index('E1')
                ref_col = split_line.index('Reference')
                contig_col = split_line.index('Contig')
                idy_col = split_line.index('IDY')
                ambig_col = split_line.index('Ambiguous')
                best_col = split_line.index('Best_group')
            elif split_line and split_line[0] == 'CONTIG':
                continue
            elif split_line and len(split_line) < 5:
                misassembled_id_to_structure[contig_id].append(line.strip())
            elif split_line and len(split_line) > 5:
                start, end, ref_name, contig_id, ambiguity, is_best = int(split_line[start_col]), int(split_line[end_col]), \
                                                           split_line[ref_col], split_line[contig_col], \
                                                           split_line[ambig_col], split_line[best_col]
                block = Alignment(name=contig_id, start=start, end=end, ref_name=ref_name, is_best_set=is_best == 'True')
                block.ambiguous = ambiguity
                if block.is_best_set:
                    aligned_blocks.append(block)
                    misassembled_id_to_structure[contig_id].append(block)

    return aligned_blocks, misassembled_id_to_structure


def parse_alignments(contigs_fpaths, contig_report_fpath_pattern):
    lists_of_aligned_blocks = []
    for contigs_fpath in contigs_fpaths:
        if contig_report_fpath_pattern:
            report_fpath = contig_report_fpath_pattern % qutils.label_from_fpath_for_fname(contigs_fpath)
            aligned_blocks, misassembled_id_to_structure = parse_nucmer_contig_report(report_fpath)
            if aligned_blocks is None:
                continue

            aligned_blocks = check_misassembled_blocks(aligned_blocks, misassembled_id_to_structure, filter_local=True)
            lists_of_aligned_blocks.append(aligned_blocks)

    if lists_of_aligned_blocks:
        max_contigs = max([len(aligned_blocks) for aligned_blocks in lists_of_aligned_blocks])
        return get_assemblies(contigs_fpaths, lists_of_aligned_blocks).assemblies, max_contigs
    else:
        return None, None


def create_alignment_plots(assembly, ref_len, output_dir):
    conf_fpath = join(output_dir, assembly.label + '.conf')
    max_gap = ref_len // 50000
    with open(conf_fpath, 'w') as out_f:
        prev_align = None
        for align in assembly.alignments:
            align.color = 'green'
            if align.misassembled:
                align.color = 'red'
            elif align.ambiguous:
                align.color = 'ppurple'
            if prev_align and prev_align.ref_name == align.ref_name and align.color == prev_align.color and \
                            max(align.start, prev_align.start) - min(align.end, prev_align.end) < max_gap:
                prev_align.start = min(align.start, prev_align.start)
                prev_align.end = max(align.end, prev_align.end)
            else:
                if prev_align:
                    out_f.write('\t'.join([prev_align.ref_name, str(prev_align.start), str(prev_align.end), 'color=' + prev_align.color]) + '\n')
                prev_align = align
        out_f.write('\t'.join([prev_align.ref_name, str(prev_align.start), str(prev_align.end), 'color=' + prev_align.color]) + '\n')
    return conf_fpath


def create_gc_plot(gc_fpath, data_dir):
    gc_values = []
    with open(gc_fpath) as f:
        for line in f:
            gc_values.append(float(line.split()[-1]))
    max_points = len(gc_values)
    min_gc, max_gc = int(min(gc_values)), int(max(gc_values))
    dst_gc_fpath = join(data_dir, 'gc.txt')
    shutil.copy(gc_fpath, dst_gc_fpath)
    return dst_gc_fpath, min_gc, max_gc, max_points


def create_coverage_plot(cov_fpath, window_size, ref_len, output_dir):
    max_points = 0
    if not cov_fpath:
        return None, max_points

    cov_by_chrom = dict()
    cov_data_fpath = join(output_dir, 'coverage.txt')
    with open(cov_fpath) as f:
        pos = 0
        for index, line in enumerate(f):
            fs = line.split()
            if line.startswith('#'):
                chrom = fs[0][1:]
                cov_by_chrom[chrom] = [[] for i in range (ref_len // window_size + 1)]
            else:
                depth = int(fs[-1])
                cov_by_chrom[chrom][pos // window_size].append(depth)
                pos += COVERAGE_FACTOR
                if pos > ref_len:
                    break

    with open(cov_data_fpath, 'w') as out_f:
        for chrom, depth_list in cov_by_chrom.items():
            for i, depths in enumerate(depth_list):
                avg_depth = sum(depths) / len(depths) if depths else 0
                out_f.write('\t'.join([chrom, str(i * window_size), str(((i + 1) * window_size)), str(avg_depth)]) + '\n')
                max_points += 1
    return cov_data_fpath, max_points


def create_mismatches_plot(assembly, window_size, ref_len, root_dir, output_dir):
    assembly_label = qutils.label_from_fpath_for_fname(assembly.fpath)
    nucmer_dirpath = join(root_dir, '..', 'contigs_reports')
    nucmer_fpath = join(create_nucmer_output_dir(nucmer_dirpath), assembly_label)
    _, _, _, _, used_snps_fpath = get_nucmer_aux_out_fpaths(nucmer_fpath)
    if not exists(used_snps_fpath):
        return None

    mismatches_fpath = join(output_dir, assembly_label + '.mismatches.txt')
    mismatch_density_by_chrom = defaultdict(lambda : [0] * (ref_len // window_size + 1))
    for line in open_gzipsafe(used_snps_fpath):
        chrom, contig, ref_pos, ref_nucl, ctg_nucl, ctg_pos = line.split('\t')
        if ref_nucl != '.' and ctg_nucl != '.':
            mismatch_density_by_chrom[chrom][int(ref_pos) // window_size] += 1
    with open(mismatches_fpath, 'w') as out_f:
        for chrom, density_list in mismatch_density_by_chrom.items():
            start, end = 0, 0
            for i, density in enumerate(density_list):
                if density == 0:
                    end = (i + 1) * window_size
                else:
                    if end:
                        out_f.write('\t'.join([chrom, str(start), str(end), '0']) + '\n')
                    out_f.write('\t'.join([chrom, str(i * window_size), str(((i + 1) * window_size)), str(density)]) + '\n')
                    start = (i + 1) * window_size
                    end = None
            out_f.write('\t'.join([chrom, str(start), str(end), '0']) + '\n')
    return mismatches_fpath


def create_genes_plot(features_containers, window_size, ref_len, output_dir):
    feature_fpaths = []
    max_points = 0
    if not features_containers:
        return feature_fpaths, max_points

    for feature_container in features_containers:
        feature_fpath = join(output_dir, feature_container.kind + '.txt')
        if len(feature_container.region_list) == 0:
            continue

        num_points = 0
        gene_density_by_chrom = defaultdict(lambda : [0] * (ref_len // window_size + 1))
        with open(feature_fpath, 'w') as out_f:
            for region in feature_container.region_list:
                chrom = region.chromosome if region.chromosome and region.chromosome in feature_container.chr_names_dict \
                    else region.seqname
                chrom = feature_container.chr_names_dict[chrom] if chrom in feature_container.chr_names_dict else None
                if not chrom:
                    continue
                for i in range(region.start // window_size, min(region.end // window_size + 1, len(gene_density_by_chrom[chrom]))):
                    if i < len(gene_density_by_chrom[chrom]):
                        gene_density_by_chrom[chrom][i] += 1
            for chrom, gene_density_list in gene_density_by_chrom.items():
                for i, density in enumerate(gene_density_list):
                    out_f.write('\t'.join([chrom, str(i * window_size), str(((i + 1) * window_size)), str(density)]) + '\n')
                    num_points += 1
        feature_fpaths.append(feature_fpath)
        max_points = max(max_points, num_points)
    return feature_fpaths, max_points


def create_genome_file(chr_lengths, output_dir):
    genome_fpath = join(output_dir, 'genome.txt')
    with open(genome_fpath, 'w') as out_f:
        for name, seq_len in chr_lengths.items():
            out_f.write('\t'.join([name, '0', str(seq_len)]) + '\n')
    return genome_fpath


def create_labels(chr_lengths, assemblies, features_containers, coverage_fpath, output_dir):
    labels_txt_fpath = join(output_dir, 'labels.txt')
    track_labels = []
    plot_idx = 0
    for i, assembly in enumerate(assemblies):
        track_labels.append(('assembly' + str(i + 1), plot_idx))
        plot_idx += 1

    for feature_container in features_containers:
        if len(feature_container.region_list) > 0:
            track_labels.append((feature_container.kind, plot_idx))
            plot_idx += 1
    if coverage_fpath:
        track_labels.append(('coverage', plot_idx))
    with open(labels_txt_fpath, 'w') as out_f:
        out_f.write(list(chr_lengths.keys())[0] + '\t0\t0\tnull\t' + ','.join(['track%d=%s' % (i, label) for label, i in track_labels]))
    labels_conf_fpath = join(output_dir, 'label.conf')
    with open(labels_conf_fpath, 'w') as out_f:
        out_f.write('z = 10\n'
                    'type = text\n'
                    'label_size = 30p\n'
                    'label_font = bold\n'
                    'label_parallel = yes\n'
                    'file = ' + labels_txt_fpath + '\n'
                    'r0 = eval(sprintf("%fr+5p", conf(conf(., track_idx)_pos)))\n'
                    'r1 = eval(sprintf("%fr+500p", conf(conf(., track_idx)_pos)))\n'
                    '<rules>\n'
                    '<rule>\n'
                    'condition = 1\n'
                    'value = eval(var(conf(., track_idx)))\n'
                    '</rule>\n'
                    '</rules>\n')
    return labels_conf_fpath, track_labels


def create_housekeeping_file(chr_lengths, max_points, root_dir, output_dir, logger):
    max_ideograms = len(chr_lengths.keys())
    template_fpath = None
    circos_bin_fpath = get_path_to_program('circos')
    if circos_bin_fpath:
        circos_dirpath = dirname(realpath(get_path_to_program('circos')))
        template_fpath = join(circos_dirpath, '..', 'libexec', 'etc', 'housekeeping.conf')
        if not is_non_empty_file(template_fpath):
            template_fpath = join(circos_dirpath, '..', 'etc', 'housekeeping.conf')

    if not is_non_empty_file(template_fpath):
        if not get_path_to_program('circos'):
            msg = 'Circos is not found.'
        else:
            msg = 'File etc/housekeeping.conf is not found.'
        logger.warning(msg + ' You will have to manually edit etc/housekeeping.conf: '
                       'set max_points_per_track to ' + str(max_points) + ' and max_ideograms to ' + str(max_ideograms))
        return '<<include %s>>\n' % join('etc', 'housekeeping.conf')

    housekeeping_fpath = join(output_dir, 'housekeeping.conf')
    with open(template_fpath) as f:
        with open(housekeeping_fpath, 'w') as out_f:
            for line in f:
                if 'max_points_per_track' in line:
                    out_f.write('max_points_per_track = %d\n' % max_points)
                elif 'max_ideograms' in line:
                    out_f.write('max_ideograms = %d\n' % max_ideograms)
                else:
                    out_f.write(line)
    return '<<include %s>>\n' % relpath(housekeeping_fpath, root_dir)


def set_window_size(ref_len):
    if ref_len > 5 * 10 ** 8:
        window_size = 20000
    elif ref_len > 3 * 10 ** 8:
        window_size = 10000
    elif ref_len > 10 ** 8:
        window_size = 5000
    elif ref_len > 10 ** 6:
        window_size = 1000
    else:
        window_size = 100
    return window_size


def create_legend(assemblies, min_gc, max_gc, features_containers, coverage_fpath, output_dir):
    legend_fpath = join(output_dir, 'legend.txt')
    with open(legend_fpath, 'w') as out_f:
        out_f.write('1) The outer circle represents reference contigs with GC (%%) heatmap [from %d%% (white) to %d%% (black)].\n' %
                    (min_gc, max_gc))
        if qconfig.is_combined_ref:
            out_f.write('Color bars help to distinguish different references.\n')

        out_f.write('2) Assemblies tracks.\n')
        for i, assembly in enumerate(assemblies):
            out_f.write('Assembly %d - %s\n' % (i, assembly.label))
        out_f.write('Assemblies tracks is combined with mismatches visualization: higher columns mean more mismatches.\n')
        if features_containers:
            out_f.write('3) User-provided genes. The darker colour mean more density of genes.\n')
        if coverage_fpath:
            out_f.write('%d) The inner circle represents read coverage histogram.\n' % 4 if features_containers else 3)
    return legend_fpath


def create_conf(ref_fpath, contigs_fpaths, contig_report_fpath_pattern, output_dir, gc_fpath, features_containers, cov_fpath, logger):
    data_dir = join(output_dir, 'data')
    if not exists(data_dir):
        os.makedirs(data_dir)

    chr_lengths = get_chr_lengths_from_fastafile(ref_fpath)
    max_len, karyotype_fpath, ideogram_fpath = create_ideogram(chr_lengths, data_dir)
    if max_len >= 10 ** 6:
        chrom_units = 10 ** 5
    elif max_len >= 10 ** 5:
        chrom_units = 10 ** 4
    else:
        chrom_units = 1000
    ticks_fpath = create_ticks_conf(chrom_units, data_dir)
    ref_len = sum(chr_lengths.values())
    window_size = set_window_size(ref_len)

    assemblies, contig_points = parse_alignments(contigs_fpaths, contig_report_fpath_pattern)
    alignments_fpaths = [create_alignment_plots(assembly, ref_len, data_dir) for assembly in assemblies]
    if not alignments_fpaths:
        return None

    gc_fpath, min_gc, max_gc, gc_points = create_gc_plot(gc_fpath, data_dir)
    feature_fpaths, gene_points = create_genes_plot(features_containers, window_size, ref_len, data_dir)
    mismatches_fpaths = [create_mismatches_plot(assembly, window_size, ref_len, output_dir, data_dir) for assembly in assemblies]
    cov_data_fpath, cov_points = create_coverage_plot(cov_fpath, window_size, ref_len, data_dir)
    max_points = max([MAX_POINTS, gc_points, gene_points, cov_points, contig_points])
    labels_fpath, track_labels = create_labels(chr_lengths, assemblies, features_containers, cov_data_fpath, data_dir)

    conf_fpath = join(output_dir, 'circos.conf')
    radius = 0.95
    plot_idx = 0
    track_intervals = [TRACK_INTERVAL] * len(assemblies)
    if feature_fpaths:
        track_intervals[-1] = BIG_TRACK_INTERVAL
        track_intervals += [TRACK_INTERVAL] * len(feature_fpaths)
    if cov_data_fpath:
        track_intervals[-1] = BIG_TRACK_INTERVAL
        track_intervals.append(TRACK_INTERVAL)
    track_intervals[-1] = BIG_TRACK_INTERVAL
    with open(conf_fpath, 'w') as out_f:
        out_f.write('<<include etc/colors_fonts_patterns.conf>>\n')
        out_f.write('<<include %s>>\n' % relpath(ideogram_fpath, output_dir))
        out_f.write('<<include %s>>\n' % relpath(ticks_fpath, output_dir))
        out_f.write('karyotype = %s\n' % relpath(karyotype_fpath, output_dir))
        out_f.write('chromosomes_units = %d\n' % chrom_units)
        out_f.write('chromosomes_display_default = yes\n')
        out_f.write('track_width = ' + str(TRACK_WIDTH) + '\n')
        for i in range(len(track_intervals)):
            out_f.write('track%d_pos = %f\n' % (i, radius))
            radius -= TRACK_WIDTH
            radius -= track_intervals[i]
        out_f.write('track%d_pos = %f\n' % (len(track_intervals), radius))
        out_f.write('<image>\n')
        out_f.write('dir = %s\n' % output_dir)
        out_f.write('file = %s\n' % circos_png_fname)
        out_f.write('png = yes\n')
        out_f.write('svg = no\n')
        out_f.write('radius = 1500p\n')
        out_f.write('angle_offset = -90\n')
        out_f.write('auto_alpha_colors = yes\n')
        out_f.write('auto_alpha_steps = 5\n')
        out_f.write('background = white\n')
        out_f.write('</image>\n')
        if qconfig.is_combined_ref:
            out_f.write('<highlights>\n')
            highlights_fpath = create_meta_highlights(chr_lengths, data_dir)
            out_f.write('<highlight>\n')
            out_f.write('file = %s\n' % relpath(highlights_fpath, output_dir))
            out_f.write('r0 = 1r - 50p\n')
            out_f.write('r1 = 1r - 30p\n')
            out_f.write('</highlight>\n')
            out_f.write('</highlights>\n')
        out_f.write(create_housekeeping_file(chr_lengths, max_points, output_dir, data_dir, logger))
        out_f.write('<plots>\n')
        out_f.write('layers_overflow = collapse\n')
        for label, i in track_labels:
            out_f.write('<plot>\n')
            out_f.write('track_idx = track%d\n' % i)
            out_f.write('<<include %s>>\n' % relpath(labels_fpath, output_dir))
            out_f.write('</plot>\n')
        for i, alignments_conf in enumerate(alignments_fpaths):
            out_f.write('<plot>\n')
            out_f.write('type = tile\n')
            out_f.write('thickness = 50p\n')
            out_f.write('stroke_thickness = 0\n')
            out_f.write('layers = 1\n')
            out_f.write('file = %s\n' % relpath(alignments_conf, output_dir))
            out_f.write('r0 = eval(sprintf("%.3fr",conf(track' + str(plot_idx) + '_pos) - conf(track_width)))\n')
            out_f.write('r1 = eval(sprintf("%.3fr",conf(track' + str(plot_idx) + '_pos)))\n')
            out_f.write('</plot>\n')
            if mismatches_fpaths and mismatches_fpaths[i]:
                out_f.write('<plot>\n')
                out_f.write('type = histogram\n')
                out_f.write('thickness = 1\n')
                out_f.write('fill_color = vlyellow\n')
                out_f.write('file = %s\n' % relpath(mismatches_fpaths[i], output_dir))
                out_f.write('r0 = eval(sprintf("%.3fr",conf(track' + str(plot_idx) + '_pos) - conf(track_width)))\n')
                out_f.write('r1 = eval(sprintf("%.3fr",conf(track' + str(plot_idx) + '_pos)))\n')
                out_f.write('</plot>\n')
            plot_idx += 1
        for feature_fpath in feature_fpaths:
            # genes plot
            out_f.write('<plot>\n')
            out_f.write('type = heatmap\n')
            out_f.write('file = %s\n' % relpath(feature_fpath, output_dir))
            out_f.write('color = ylorbr-9\n')
            out_f.write('r0 = eval(sprintf("%.3fr",conf(track' + str(plot_idx) + '_pos) - conf(track_width)))\n')
            out_f.write('r1 = eval(sprintf("%.3fr",conf(track' + str(plot_idx) + '_pos)))\n')
            out_f.write('</plot>\n')
            plot_idx += 1
        if cov_data_fpath:
            # coverage plot
            out_f.write('<plot>\n')
            out_f.write('type = histogram\n')
            out_f.write('thickness = 1\n')
            out_f.write('file = %s\n' % relpath(cov_data_fpath, output_dir))
            out_f.write('fill_color = vlblue\n')
            out_f.write('r0 = eval(sprintf("%.3fr",conf(track' + str(plot_idx) + '_pos) - conf(track_width)))\n')
            out_f.write('r1 = eval(sprintf("%.3fr",conf(track' + str(plot_idx) + '_pos)))\n')
            out_f.write('</plot>\n')
            plot_idx += 1
        # GC plot
        out_f.write('<plot>\n')
        out_f.write('type = heatmap\n')
        out_f.write('file = %s\n' % relpath(gc_fpath, output_dir))
        out_f.write('color = greys-6\n')
        out_f.write('scale_log_base = 1.5\n')
        out_f.write('r0 = 1r - 29p\n')
        out_f.write('r1 = 1r - 1p\n')
        out_f.write('</plot>\n')
        out_f.write('</plots>\n')

    circos_legend_fpath = create_legend(assemblies, min_gc, max_gc, features_containers, cov_data_fpath, output_dir)
    return conf_fpath, circos_legend_fpath


def do(ref_fpath, contigs_fpaths, contig_report_fpath_pattern, gc_fpath, features_containers, cov_fpath, output_dir, logger):
    if not exists(output_dir):
        os.makedirs(output_dir)
    conf_fpath, circos_legend_fpath = create_conf(ref_fpath, contigs_fpaths, contig_report_fpath_pattern, output_dir, gc_fpath, features_containers, cov_fpath, logger)
    circos_exec = get_path_to_program('circos')
    if not circos_exec:
        logger.warning('Circos is not installed!\n'
                       'If you want to create Circos plots, install Circos as described at http://circos.ca/tutorials/lessons/configuration/distribution_and_installation '
                       'and run the following command:\n circos -conf ' + conf_fpath + '.\n '
                       'The plot annotation is saved to ' + circos_legend_fpath)
        return None, None

    cmdline = [circos_exec, '-conf', conf_fpath]
    log_fpath = join(output_dir, 'circos.log')
    err_fpath = join(output_dir, 'circos.err')
    circos_png_fpath = join(output_dir, circos_png_fname)
    return_code = qutils.call_subprocess(cmdline, stdout=open(log_fpath, 'w'), stderr=open(err_fpath, 'w'))
    if return_code == 0 and is_non_empty_file(circos_png_fpath):
        return circos_png_fpath, circos_legend_fpath
    else:
        logger.warning('  Circos diagram was not created. See ' + log_fpath + ' and ' + err_fpath + ' for details')
        return None, None

